"""Two `kb keys` write verbs must not lose each other's update.

Every write verb (`create`, `revoke`, `rotate`, `prune`) reads the key file, edits
the records in memory and replaces the file. With no lock between the read and the
replace, two overlapping verbs both start from the same bytes and the second
replace throws the first one away. The worst case is a revoke: a `create` that
loaded the file before the revoke and saved after it writes the revoked key back
as live, and the next `kb serve` start serves it.

These tests hold one verb between its load and its save, run a second verb in that
window, and read the key file afterwards. A verb that does not wait for the lock
shows up as a missing effect in the final file, which is what an operator sees.

Every test names its key file through `$CONTEXTLAKE_KEYS_FILE` under `tmp_path` and
moves HOME there. The real key file is never named. Nothing here calls
`monkeypatch.undo()`: it would also undo conftest's HOME redirect.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from contextlake import cli
from contextlake.kb import keyfile
from contextlake.kb import keys as keys_mod
from contextlake.kb.cmds import keys_cmd

posix_only = pytest.mark.skipif(not keyfile.POSIX, reason="mode and owner checks are POSIX")

SRC = pathlib.Path(keyfile.__file__).resolve().parents[2]
KEYS_CMD_SOURCE = pathlib.Path(keys_cmd.__file__).resolve()


@pytest.fixture
def keys_path(tmp_path, monkeypatch):
    """The key file under `tmp_path`, named by the environment tier, HOME moved."""
    monkeypatch.setenv("HOME", str(tmp_path / "scratch-home"))
    path = tmp_path / "keys" / "mcp-keys.json"
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o700)
    monkeypatch.setenv(keyfile.KEYS_FILE_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def _no_key_display(monkeypatch):
    """The display-once path is not under test here, and it prints a live key.
    Silenced so a failing test never carries a key in its captured output."""
    monkeypatch.setattr(keys_cmd, "_emit_key", lambda key, *, print_key: None)


@dataclass(frozen=True)
class Seed:
    victim_id: str
    old_id: str
    victim_key: str = field(repr=False)  # a live key: never in a failure message


def _seed(path):
    """A live key named `victim` and a long-revoked one named `old`.

    `old` is what `prune --before 2021-01-01` removes. The victim's plaintext is
    kept so a test can present it to a keyring the way a client would.
    """
    records = []
    victim, victim_key = keys_mod.create(records, "victim", grant_version="test")
    old, _ = keys_mod.create(records, "old", grant_version="test")
    keys_mod.revoke(records, old, now=datetime(2020, 1, 1, tzinfo=timezone.utc))
    keyfile.write_document(path, records)
    return Seed(victim.id, old.id, victim_key)


def _records(path):
    """Every record in the file, by id. A rotate adds a second record with the
    same NAME, so a name is not a key."""
    return {r["id"]: r for r in json.loads(path.read_text())["keys"]}


def _ns(**fields):
    return SimpleNamespace(json=False, config=None, **fields)


def _plan(verb, tag, ids):
    """The handler, its args, and a check that the verb's effect is in the file."""
    victim_id, old_id = ids.victim_id, ids.old_id
    if verb == "create":
        name = f"new-{tag}"
        return (keys_cmd._cmd_create, _ns(name=name),
                lambda recs: any(r["name"] == name and not r["revoked_at"]
                                 for r in recs.values()))
    if verb == "revoke":
        return (keys_cmd._cmd_revoke, _ns(name=victim_id, reason="test"),
                lambda recs: bool(recs[victim_id]["revoked_at"]))
    if verb == "rotate":
        return (keys_cmd._cmd_rotate, _ns(name=victim_id),
                lambda recs: bool(recs[victim_id]["rotated_to"]))
    if verb == "prune":
        return (keys_cmd._cmd_prune, _ns(before="2021-01-01"),
                lambda recs: old_id not in recs)
    raise AssertionError(verb)


def _interleave(monkeypatch, first, second):
    """Run `first` and hold it after its load, run `second` in that window.

    Returns `(events, first_result, second_result, second_loaded_while_held)`.
    `events` is every `_load` and `_save` as `(thread, phase)` in the order they
    happened, so a test can say which verb waited for which.
    """
    events = []
    held = threading.Event()
    release = threading.Event()
    real_load, real_save = keys_cmd._load, keys_cmd._save

    def load(path, *, write):
        events.append((threading.current_thread().name, "load"))
        return real_load(path, write=write)

    def save(path, records):
        name = threading.current_thread().name
        events.append((name, "save"))
        if name == "first":
            held.set()
            if not release.wait(20):
                raise AssertionError("the test never released the held verb")
        real_save(path, records)
        events.append((name, "saved"))

    monkeypatch.setattr(keys_cmd, "_load", load)
    monkeypatch.setattr(keys_cmd, "_save", save)

    results = {}

    def worker(name, handler, args):
        try:
            results[name] = ("ok", handler(args))
        except BaseException as exc:  # reported by the assertions below
            results[name] = ("error", exc)

    t_first = threading.Thread(target=worker, name="first", args=("first", *first))
    t_second = threading.Thread(target=worker, name="second", args=("second", *second))
    t_first.start()
    try:
        assert held.wait(20), "the first verb never reached its save"
        t_second.start()
        # A verb with nothing in its way finishes in milliseconds. One that
        # waits for the lock is still blocked when this returns.
        t_second.join(0.5)
        second_loaded_while_held = ("second", "load") in events
    finally:
        release.set()
    for t in (t_first, t_second):
        if t.is_alive() or t.ident is not None:
            t.join(30)
    assert not t_first.is_alive() and not t_second.is_alive(), "a verb never finished"
    return events, results["first"], results["second"], second_loaded_while_held


PAIRS = [
    ("create", "revoke"),   # the revoked key comes back
    ("revoke", "create"),   # the new key is lost
    ("rotate", "create"),
    ("prune", "create"),
    ("create", "create"),
    ("create", "prune"),    # the pruned record comes back
]


@pytest.mark.parametrize("first,second", PAIRS)
def test_overlapping_write_verbs_both_land(keys_path, monkeypatch, first, second):
    ids = _seed(keys_path)
    first_plan = _plan(first, "a", ids)
    second_plan = _plan(second, "b", ids)

    events, r_first, r_second, second_loaded = _interleave(
        monkeypatch, first_plan[:2], second_plan[:2])

    assert r_first == ("ok", 0) and r_second == ("ok", 0)
    recs = _records(keys_path)
    assert first_plan[2](recs), f"the {first} verb's change is not in the key file"
    assert second_plan[2](recs), f"the {second} verb's change is not in the key file"
    # The second verb waited: it did not read the file while the first held it,
    # and its read came after the first verb's write.
    assert not second_loaded, "the second verb read the file while the first held the lock"
    assert events.index(("second", "load")) > events.index(("first", "saved"))


def test_a_revoked_key_stays_revoked_when_a_create_overlaps(keys_path, monkeypatch):
    """The headline case, spelled out: revoke the key, overlap a create, restart.

    Reads the outcome the way a client meets it. A new `Keyring.load` is what a
    restarted `kb serve` builds, and `resolve` is what the auth gate calls with the
    key a client presents.
    """
    ids = _seed(keys_path)
    first = _plan("create", "a", ids)
    second = _plan("revoke", "b", ids)

    _interleave(monkeypatch, first[:2], second[:2])

    ring = keyfile.Keyring.load(keys_path)
    _, state = ring.resolve(ids.victim_key)
    assert state == "revoked", f"a restarted server admits the revoked key ({state})"
    assert _records(keys_path)[ids.victim_id]["revoked_at"], "live again on disk"
    assert ring.live_count() == 1  # only the key the create minted


# --- the lock itself ---------------------------------------------------------


def _lock_file(path):
    return path.with_name(path.name + ".lock")


@posix_only
def test_the_lock_file_is_0600_next_to_the_key_file(keys_path):
    with keyfile.write_lock(keys_path):
        lock = _lock_file(keys_path)
        assert lock.is_file()
        assert lock.parent == keys_path.parent
        assert lock.stat().st_mode & 0o777 == 0o600


@posix_only
def test_a_lock_file_with_loose_bits_is_tightened(keys_path):
    lock = _lock_file(keys_path)
    lock.write_text("")
    lock.chmod(0o666)
    with keyfile.write_lock(keys_path):
        assert lock.stat().st_mode & 0o777 == 0o600


@posix_only
def test_a_group_writable_directory_is_refused_and_gets_no_lock_file(keys_path):
    keys_path.parent.chmod(0o770)
    with pytest.raises(keyfile.KeyFileError):
        with keyfile.write_lock(keys_path):
            pytest.fail("the lock was taken in a directory the key file is refused in")
    assert not _lock_file(keys_path).exists()


@posix_only
def test_a_missing_directory_is_made_at_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "scratch-home"))
    path = tmp_path / "a" / "b" / "mcp-keys.json"
    with keyfile.write_lock(path):
        pass
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.parent.parent.stat().st_mode & 0o777 == 0o700


@posix_only
def test_a_symlink_at_the_lock_path_is_refused_not_followed(keys_path, tmp_path):
    victim = tmp_path / "somewhere-else"
    _lock_file(keys_path).symlink_to(victim)
    with pytest.raises(keyfile.KeyFileError):
        with keyfile.write_lock(keys_path):
            pytest.fail("the lock followed a symlink")
    assert not victim.exists(), "opening the lock created the symlink's target"


@posix_only
def test_a_fifo_at_the_lock_path_is_refused(keys_path):
    os.mkfifo(_lock_file(keys_path))
    with pytest.raises(keyfile.KeyFileError):
        with keyfile.write_lock(keys_path):
            pytest.fail("the lock accepted a fifo")


def test_the_lock_file_is_kept_after_release(keys_path):
    """Deleting a lock file lets two holders lock two different inodes."""
    with keyfile.write_lock(keys_path):
        inode = _lock_file(keys_path).stat().st_ino
    assert _lock_file(keys_path).stat().st_ino == inode
    with keyfile.write_lock(keys_path):
        assert _lock_file(keys_path).stat().st_ino == inode


def test_a_busy_lock_reports_instead_of_hanging(keys_path):
    with keyfile.write_lock(keys_path):
        started = time.monotonic()
        with pytest.raises(keyfile.KeyFileBusy) as caught:
            with keyfile.write_lock(keys_path, timeout=0.3):
                pytest.fail("two holders at once")
        waited = time.monotonic() - started
    assert 0.3 <= waited < 5
    message = str(caught.value)
    assert str(_lock_file(keys_path)) in message
    assert "retry" in message.lower()
    assert isinstance(caught.value, keyfile.KeyFileError)  # `cmd_keys` maps it to exit 1


def test_a_lock_that_cannot_be_taken_for_another_reason_is_reported(
        keys_path, monkeypatch):
    """A filesystem with no file locks is not a busy lock, and not a traceback."""
    def refuse(fd):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(keyfile, "_try_lock", refuse)
    with pytest.raises(keyfile.KeyFileError) as caught:
        with keyfile.write_lock(keys_path):
            pytest.fail("the body ran without a lock")
    assert not isinstance(caught.value, keyfile.KeyFileBusy)
    assert str(_lock_file(keys_path)) in str(caught.value)


def test_a_lock_that_is_released_can_be_taken_again(keys_path):
    with keyfile.write_lock(keys_path):
        pass
    with keyfile.write_lock(keys_path, timeout=0.3):
        pass


def test_the_lock_is_released_when_the_body_raises(keys_path):
    with pytest.raises(RuntimeError):
        with keyfile.write_lock(keys_path):
            raise RuntimeError("body failed")
    with keyfile.write_lock(keys_path, timeout=0.3):
        pass


def test_the_default_wait_is_bounded():
    assert 0 < keyfile.LOCK_TIMEOUT <= 60


# --- two real processes ------------------------------------------------------

HOLDER = """
import pathlib, sys, time
from contextlake.kb import keyfile
with keyfile.write_lock(sys.argv[1]):
    pathlib.Path(sys.argv[2]).write_text("held")
    time.sleep(120)
"""


def _child_env(tmp_path, keys_path):
    env = {k: v for k, v in os.environ.items() if k not in ("HF_HOME", "PYTHONPATH")}
    env["PYTHONPATH"] = str(SRC)
    env["HOME"] = str(tmp_path / "scratch-home")
    env[keyfile.KEYS_FILE_ENV] = str(keys_path)
    return env


@pytest.fixture
def holder(tmp_path, keys_path):
    """A second process that holds the key file's write lock until it is killed."""
    flag = tmp_path / "holder-ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(keys_path), str(flag)],
        env=_child_env(tmp_path, keys_path),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not flag.exists():
            if proc.poll() is not None:
                raise AssertionError(
                    f"the holder exited early: {proc.stderr.read().decode()[-500:]}")
            if time.monotonic() > deadline:
                raise AssertionError("the holder never took the lock")
            time.sleep(0.05)
        yield proc
    finally:
        proc.kill()
        proc.wait(10)
        proc.stderr.close()


def test_another_process_holding_the_lock_makes_a_write_verb_exit_1(
        keys_path, holder, monkeypatch, capsys):
    ids = _seed(keys_path)
    before = keys_path.read_bytes()
    monkeypatch.setattr(keyfile, "LOCK_TIMEOUT", 0.5)
    started = time.monotonic()

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["kb", "keys", "revoke", ids.victim_id])

    waited = time.monotonic() - started
    shown = capsys.readouterr()
    assert exit_info.value.code == 1
    assert waited < 10
    assert _lock_file(keys_path).name in shown.out + shown.err
    assert keys_path.read_bytes() == before, "a refused verb changed the key file"


def test_the_busy_refusal_is_a_key_file_error_in_json(
        keys_path, holder, monkeypatch, capsys):
    ids = _seed(keys_path)
    monkeypatch.setattr(keyfile, "LOCK_TIMEOUT", 0.5)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["kb", "keys", "revoke", ids.victim_id, "--json"])

    assert exit_info.value.code == 1
    document = json.loads(capsys.readouterr().out)
    assert document["error"] == "key_file_error"


def test_a_crashed_holder_leaves_no_stale_lock(keys_path, holder):
    holder.kill()
    holder.wait(10)
    started = time.monotonic()
    with keyfile.write_lock(keys_path, timeout=5):
        pass
    assert time.monotonic() - started < 5


def test_a_verb_that_waited_sees_the_other_processs_result(
        tmp_path, keys_path, monkeypatch):
    """The waiting verb runs once the holder lets go, and reads what it wrote."""
    ids = _seed(keys_path)
    flag = tmp_path / "ready"
    release = tmp_path / "release"
    script = (
        "import pathlib, sys, time\n"
        "from contextlake.kb import keyfile, keys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "with keyfile.write_lock(path):\n"
        "    doc = keyfile.load_document(path)\n"
        "    records = [keys.KeyRecord.from_dict(d) for d in doc.keys]\n"
        "    keys.create(records, 'made-by-holder', grant_version='test')\n"
        "    pathlib.Path(sys.argv[2]).write_text('held')\n"
        "    while not pathlib.Path(sys.argv[3]).exists():\n"
        "        time.sleep(0.05)\n"
        "    keyfile.write_document(path, records)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(keys_path), str(flag), str(release)],
        env=_child_env(tmp_path, keys_path),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not flag.exists():
            assert proc.poll() is None, proc.stderr.read().decode()[-500:]
            assert time.monotonic() < deadline, "the holder never took the lock"
            time.sleep(0.05)
        releaser = threading.Timer(1.0, release.write_text, args=("go",))
        releaser.start()
        try:
            args = _ns(name=ids.victim_id, reason="test")
            assert keys_cmd._cmd_revoke(args) == 0
        finally:
            releaser.cancel()
            release.write_text("go")
        assert proc.wait(30) == 0
    finally:
        proc.kill()
        proc.wait(10)
        proc.stderr.close()
    recs = _records(keys_path)
    assert any(r["name"] == "made-by-holder" for r in recs.values()), (
        "the holder's create was lost")
    assert recs[ids.victim_id]["revoked_at"], "the revoke did not land"


# --- Windows -----------------------------------------------------------------


class _FakeMsvcrt(types.SimpleNamespace):
    """The part of `msvcrt.locking` the Windows branch calls, so it runs on any OS."""

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self, fail_with=None):
        super().__init__(calls=[], fail_with=fail_with)

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if self.fail_with is not None and mode == self.LK_NBLCK:
            raise OSError(self.fail_with, "Permission denied")


@pytest.fixture
def lock_fd(tmp_path):
    fd = os.open(tmp_path / "x.lock", os.O_RDWR | os.O_CREAT, 0o600)
    os.lseek(fd, 5, os.SEEK_SET)  # a stray offset the lock must not depend on
    yield fd
    os.close(fd)


def test_windows_locks_one_byte_at_offset_zero_without_waiting(monkeypatch, lock_fd):
    fake = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    assert keyfile._try_lock_windows(lock_fd) is True
    assert fake.calls == [(fake.LK_NBLCK, 1, 0)]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EDEADLK])
def test_windows_lock_violation_means_busy(monkeypatch, lock_fd, code):
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(fail_with=code))
    assert keyfile._try_lock_windows(lock_fd) is False


def test_windows_other_errors_are_not_read_as_busy(monkeypatch, lock_fd):
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(fail_with=errno.EBADF))
    with pytest.raises(OSError):
        keyfile._try_lock_windows(lock_fd)


def test_keyfile_imports_where_fcntl_does_not_exist(tmp_path):
    """The module must import on Windows, where there is no `fcntl`."""
    code = ("import sys; sys.modules['fcntl'] = None; "
            "import contextlake.kb.keyfile")
    env = {k: v for k, v in os.environ.items() if k not in ("HF_HOME", "PYTHONPATH")}
    env.update(PYTHONPATH=str(SRC), HOME=str(tmp_path))
    done = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-500:]


# --- every writer takes the lock ---------------------------------------------


def _calls_under_lock():
    """Each `_load(write=True)` and `_save` call in keys_cmd, with whether a
    `with keyfile.write_lock(...)` encloses it. Parsed, so a mention in a comment
    or a docstring cannot satisfy it."""
    tree = ast.parse(KEYS_CMD_SOURCE.read_text(encoding="utf-8"))
    found = []

    def is_lock(item):
        call = item.context_expr
        return (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "write_lock"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "keyfile")

    def visit(node, locked, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
            locked = False
        if isinstance(node, ast.With) and any(is_lock(i) for i in node.items):
            locked = True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            writes = node.func.id == "_save" or (
                node.func.id == "_load"
                and any(k.arg == "write" and isinstance(k.value, ast.Constant)
                        and k.value.value is True for k in node.keywords))
            if writes:
                found.append((function, node.func.id, locked))
        for child in ast.iter_child_nodes(node):
            visit(child, locked, function)

    visit(tree, False, None)
    return found


def test_every_write_verb_holds_the_lock_from_load_to_save():
    found = _calls_under_lock()
    writers = {function for function, name, _ in found if name == "_save"}
    # The four verbs that write. A new writer shows up here and has to be locked.
    assert writers == {"_cmd_create", "_cmd_revoke", "_cmd_rotate", "_cmd_prune"}
    unlocked = [(f, n) for f, n, locked in found if not locked]
    assert unlocked == [], f"outside `with keyfile.write_lock(...)`: {unlocked}"
    loads = {f for f, n, _ in found if n == "_load"}
    assert loads == writers, "a write verb loads outside the lock, or never loads"
