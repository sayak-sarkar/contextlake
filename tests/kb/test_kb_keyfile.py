"""Guards for `kb/keyfile.py`: where the MCP API keys live, how they are written,
and who may read them (epic 4, S4.2.3 and S4.2.4).

Nothing here is a real credential. Every key value comes from :func:`fake_key`,
which builds an obviously-fake 57-character string containing the literal
``NOTAREALKEY``, and every file written lands under pytest's ``tmp_path``.

Most tests inject :class:`FakeKeys` for ``contextlake.kb.keys``, so a fixture key
is an obviously-fake constant rather than a value from a real encoder. Three
tests take the DEFAULT import path with no injection at all
(``test_the_real_keys_module_round_trips_through_the_file``,
``test_the_keys_module_contract_matches_the_real_module`` and
``test_no_plaintext_key_reaches_the_file``), because a seam every test stubs is a
seam nothing proves: the wrong symbol names would ship green, and a fixture that
builds a record by hand cannot show what the record builder puts in it.
Their key comes from ``keys.key_from_bytes`` over a fixed public byte pattern,
never ``keys.mint()``, so no test writes a drawn secret anywhere.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contextlake.kb import config as kb_config
from contextlake.kb import keyfile

POSIX_ONLY = pytest.mark.skipif(os.name != "posix",
                                reason="mode bits mean nothing off POSIX")

BASE62 = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def fake_key(tag: str) -> str:
    """An obviously fake, correctly shaped key. 8 + 43 + 6 = 57 characters.

    The body spells NOTAREALKEY so no scanner, reviewer or reader can mistake a
    value in this file for a credential.

    Padded with "z", which no tag uses: padding with "0" made `fake_key("P0")`
    and `fake_key("P00")` the same 57 characters, so a 1,000-record fixture held
    duplicate digests.
    """
    body = ("NOTAREALKEY" + tag).ljust(43, "z")[:43]
    assert len(body) == 43 and set(body) <= BASE62
    return "ctxlake_" + body + "FAKE00"


class FakeRecord:
    """Stands in for the S4.2.2 record. Counts the reads of the two attributes
    the lookup path touches, so a test can tell "one record" from "all of them"."""

    def __init__(self, data, touched):
        self.id = data["id"]
        self._digest = data["digest"]
        self._revoked_at = data.get("revoked_at")
        self._expires_at = data.get("expires_at")
        self._touched = touched

    @property
    def digest(self):
        self._touched.append(self.id)
        return self._digest

    def state(self, now):
        self._touched.append(self.id)
        if self._revoked_at:
            return "revoked"
        if self._expires_at and _parse(self._expires_at) <= now:
            return "expired"
        return "live"


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class FakeKeys:
    """The three symbols `keyfile.KEYS_MODULE_CONTRACT` names, and nothing else."""

    __name__ = "fake contextlake.kb.keys"

    def __init__(self):
        self.touched: list[str] = []
        self.format_calls = 0
        self.digest_calls = 0

    def check_format(self, value) -> bool:
        self.format_calls += 1
        return (isinstance(value, str) and len(value) == 57
                and value.startswith("ctxlake_") and set(value[8:]) <= BASE62)

    def digest(self, key: str) -> str:
        self.digest_calls += 1
        return hashlib.sha256(key.encode("ascii")).hexdigest()

    @property
    def KeyRecord(self):  # noqa: N802 - the real module's class name
        touched = self.touched

        class _Bound:
            @staticmethod
            def from_dict(data):
                return FakeRecord(data, touched)

        return _Bound


def record(tag, *, revoked_at=None, expires_at=None):
    """A key file record for `fake_key(tag)`, plus the key itself."""
    key = fake_key(tag)
    return key, {
        "id": f"k_{tag}",
        "name": f"fixture {tag}",
        "digest": hashlib.sha256(key.encode("ascii")).hexdigest(),
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": expires_at,
        "revoked_at": revoked_at,
        "revoked_reason": None,
        "rotated_from": None,
        "rotated_to": None,
        "grant_version": "8.13.0",
    }


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def keys():
    return FakeKeys()


@pytest.fixture
def counted():
    """Zeroed accessor counters plus a trace of (op, path) for every touch.

    The trace matters: a bare count cannot tell a stat on the key file from a
    stat on its parent, and criterion 11 pins the per-request stat to the key
    file only.
    """
    trace: list[tuple[str, str]] = []
    keyfile.reset_counters(trace)
    yield trace
    keyfile.reset_counters(None)


def make_dir(path: Path, mode: int) -> Path:
    """Create a directory AT `mode`, verified.

    `mkdir(mode=...)` is masked by the developer's umask, so the mode a test
    thinks it set is not the mode on disk. chmod afterwards, then read it back:
    a fixture that is not the mode under test proves nothing.
    """
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    assert os.stat(path).st_mode & 0o777 == mode, f"fixture dir is not {mode:04o}"
    return path


def write_file(path: Path, payload: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(payload)
    os.chmod(path, mode)
    assert os.stat(path).st_mode & 0o777 == mode, f"fixture file is not {mode:04o}"
    return path


def document(records, version: int = keyfile.SCHEMA_VERSION) -> bytes:
    return json.dumps({"version": version, "keys": list(records)},
                      indent=2, sort_keys=True).encode("utf-8")


def atomic_replace(path: Path, payload: bytes) -> None:
    """Write `payload` the way the module writes: a temp sibling then os.replace,
    so the path lands a NEW INODE. Used by the st_ino tests."""
    tmp = path.with_name(path.name + ".probe")
    tmp.write_bytes(payload)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@pytest.fixture
def installed(tmp_path, keys):
    """A 0755 parent holding a 0600 key file with two live keys. This is the
    shape of a WORKING install: 0755 is what discriminates the 0o022 parent mask
    from the 0o077 one, and a 0700 fixture would let that break-test through."""
    home = make_dir(tmp_path / "dot-contextlake", 0o755)
    key_a, rec_a = record("A")
    key_b, rec_b = record("B")
    path = write_file(home / "mcp-keys.json", document([rec_a, rec_b]), 0o600)
    return {"path": path, "dir": home, "keys": keys,
            "key_a": key_a, "key_b": key_b, "rec_a": rec_a, "rec_b": rec_b}


# ==========================================================================
# S4.2.3 -- the write
# ==========================================================================


@POSIX_ONLY
def test_the_key_file_is_created_0600_and_the_parent_0700(tmp_path):
    path = tmp_path / "nested" / "mcp-keys.json"
    _, rec = record("A")
    keyfile.write_document(path, [rec])
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


@POSIX_ONLY
def test_no_chmod_touches_the_key_file_path(tmp_path, monkeypatch):
    """The mode is set at creation, on the temp sibling, never with a chmod after
    the rename: os.replace keeps the temp file's mode, and a later chmod leaves a
    window in which the file is world-readable.

    Both halves asserted. A module that chmods nothing at all scores 0 on the key
    file too, so the parent is the positive control.

    The parent half watches `os.fchmod`, not `os.chmod`, and that is the point of
    the change it is pinned to: a path-based chmod FOLLOWS a symlinked parent and
    changes the mode of the target. The descriptor is identified by its
    (st_dev, st_ino) rather than by a path, because a descriptor has no path."""
    paths: list[str] = []
    descriptors: list[tuple[int, int]] = []
    real_chmod, real_fchmod = os.chmod, os.fchmod
    monkeypatch.setattr(os, "chmod",
                        lambda p, m, *a, **k: (paths.append(str(p)),
                                               real_chmod(p, m))[1])

    def spy_fchmod(fd, mode):
        st = os.fstat(fd)
        descriptors.append((st.st_dev, st.st_ino))
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", spy_fchmod)
    path = tmp_path / "dir" / "mcp-keys.json"
    _, rec = record("A")
    keyfile.write_document(path, [rec])
    assert paths == [], "the mode was set with a path-based chmod"
    parent = os.stat(path.parent)
    assert descriptors == [(parent.st_dev, parent.st_ino)]
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_a_failed_replace_leaves_the_original_bytes_intact(tmp_path, monkeypatch):
    home = make_dir(tmp_path / "d", 0o700)
    _, old = record("A")
    original = document([old])
    path = write_file(home / "mcp-keys.json", original, 0o600)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    _, new = record("B")
    with pytest.raises(OSError):
        keyfile.write_document(path, [old, new])
    assert path.read_bytes() == original
    # And the temp sibling is gone. Its name now carries 16 random hex
    # characters, so a leftover cannot be reused by the next write and one 0600
    # file per failed write would sit next to the key file forever.
    assert sorted(p.name for p in home.iterdir()) == ["mcp-keys.json"]


def test_no_plaintext_key_reaches_the_file(tmp_path):
    """Scanned over the RAW BYTES after three creates, driven through
    `keys.create()`.

    `create()` is the ONE function that holds a plaintext key: it takes or draws
    the value, computes the digest from it, and builds the record the writer
    persists. A test that builds the record by hand never lets the plaintext into
    that scope, so its assertion is carried by its own fixture: a `create()` that
    stashed the key on the record, or a `to_dict()` that shipped it, would pass.
    That is what this test did for two rounds, while claiming to prove the
    opposite.

    `key=` is supplied rather than letting `create()` draw one, because no test
    here may write a value from `mint()` anywhere. The key comes from
    `key_from_bytes` over fixed public bytes. `create()` holds it either way: it
    is the parameter the digest is computed from.

    The body is scanned as well as the whole key. A leak that stored the 43
    random characters without the prefix and the checksum is the same secret on
    disk, and a whole-string scan misses it.

    The digest assertion is the positive control: without it the test passes on
    an empty file, or on a file the test never actually wrote."""
    from contextlake.kb import keys as real

    records: list = []
    made = []
    for index in range(3):
        seed = f"NOT-A-REAL-KEY-plaintext-scan-{index}".encode()[:32].ljust(32, b"0")
        rec, key = real.create(records, f"fixture {index}",
                               key=real.key_from_bytes(seed))
        made.append((key, rec))
    assert len({key for key, _ in made}) == 3, "the three fixture keys are not distinct"
    assert len(records) == 3, "create() did not append; the writer gets nothing"

    path = tmp_path / "d" / "mcp-keys.json"
    keyfile.write_document(path, records)
    raw = path.read_bytes()
    for key, rec in made:
        body = key[len(real.KEY_PREFIX):len(real.KEY_PREFIX) + real.BODY_LEN]
        assert raw.count(key.encode("ascii")) == 0
        assert raw.count(body.encode("ascii")) == 0
        assert raw.count(rec.digest.encode("ascii")) == 1


# ==========================================================================
# S4.2.3 -- where the file lives
# ==========================================================================


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """A cwd with no ancestor config, and a global config that does not exist.

    Without this the four-tier test reads the developer's own ~/.contextlake."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(kb_config, "GLOBAL_CONFIG", str(tmp_path / "global-kb.toml"))
    return work


def test_the_four_location_tiers_resolve_in_order(tmp_path, isolated_config,
                                                  monkeypatch):
    cli = tmp_path / "from-cli.json"
    env_path = tmp_path / "from-env.json"
    cfg_path = tmp_path / "from-config.json"
    global_cfg = tmp_path / "global-kb.toml"
    global_cfg.write_text(f'[serve]\nkeys_file = "{cfg_path}"\n')
    env = {keyfile.KEYS_FILE_ENV: str(env_path)}

    # 1. --keys-file beats the env var (and everything below it).
    assert keyfile.resolve_keys_file(str(cli), env=env) == cli
    # 2. the env var beats [serve] keys_file.
    assert keyfile.resolve_keys_file(None, env=env) == env_path
    # 3. [serve] keys_file beats the default.
    assert keyfile.resolve_keys_file(None, env={}) == cfg_path
    # 4. the default, when nothing else is set.
    global_cfg.unlink()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert keyfile.resolve_keys_file(None, env={}) == keyfile.default_keys_file()


def test_a_local_config_cannot_point_at_a_key_file(tmp_path, isolated_config,
                                                   monkeypatch):
    """`.contextlake.kb.toml` is found by walking up from the cwd and is designed
    to sit inside a repository checkout, so anything it names gets committed. A
    file found by directory search must never be able to mint an identity."""
    planted = tmp_path / "planted-keys.json"
    (isolated_config / kb_config.LOCAL_CONFIG).write_text(
        f'[serve]\nkeys_file = "{planted}"\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    said: list[str] = []

    resolved = keyfile.resolve_keys_file(None, env={}, warn=said.append)

    assert resolved != planted
    assert resolved == keyfile.default_keys_file()
    assert len(said) == 1
    assert kb_config.LOCAL_CONFIG in said[0] and "IGNORED" in said[0]


def test_naming_that_same_local_config_with_config_honours_it(tmp_path,
                                                              isolated_config):
    """The other direction, so the gate is not "refuse every config file".

    `--config PATH` is the user saying "I meant this file", which is exactly the
    provenance `trust.is_privileged_source` grants. Without this case a tier-3
    implementation that always returns None passes the test above."""
    named = tmp_path / "named-keys.json"
    local = isolated_config / kb_config.LOCAL_CONFIG
    local.write_text(f'[serve]\nkeys_file = "{named}"\n')
    said: list[str] = []

    resolved = keyfile.resolve_keys_file(None, env={}, config_path=str(local),
                                         warn=said.append)

    assert resolved == named
    assert said == []


# ==========================================================================
# S4.2.3 -- the load-failure split
# ==========================================================================


def test_an_absent_key_file_loads_an_empty_keyring(tmp_path, keys):
    """Absent is the ONLY case that lets cmd_serve mint. It must not raise."""
    ring = keyfile.Keyring.load(tmp_path / "nothing-here.json", keys_module=keys)
    assert ring.present is False
    assert len(ring) == 0
    assert ring.resolve(fake_key("A")) is None


def test_an_unparseable_key_file_refuses_to_load(tmp_path, keys):
    home = make_dir(tmp_path / "d", 0o755)
    path = write_file(home / "mcp-keys.json", b"{not json at all", 0o600)
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(path, keys_module=keys)
    assert str(path) in str(excinfo.value)


def test_a_future_schema_version_is_refused(tmp_path, keys):
    home = make_dir(tmp_path / "d", 0o755)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json",
                      document([rec], version=keyfile.SCHEMA_VERSION + 1), 0o600)
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(path, keys_module=keys)
    message = str(excinfo.value)
    assert str(keyfile.SCHEMA_VERSION + 1) in message
    assert str(keyfile.SCHEMA_VERSION) in message


def test_a_key_file_with_no_version_is_refused(tmp_path, keys):
    """A newer contextlake writes a field an older one does not know. Read as
    schema 1 the field is dropped, and if it is revoked_at a revoked key goes
    live again with nothing raised."""
    home = make_dir(tmp_path / "d", 0o755)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json",
                      json.dumps({"keys": [rec]}).encode("utf-8"), 0o600)
    with pytest.raises(keyfile.KeyFileError):
        keyfile.Keyring.load(path, keys_module=keys)


@POSIX_ONLY
def test_group_bits_on_the_key_file_refuse_to_load(tmp_path, keys):
    home = make_dir(tmp_path / "d", 0o755)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o640)
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(path, keys_module=keys)
    message = str(excinfo.value)
    assert str(path) in message
    assert "0640" in message


@POSIX_ONLY
def test_a_world_writable_parent_refuses_to_load(tmp_path, keys):
    """The finding a file-only check misses. The file is 0600 and perfect; the
    directory is 0777, so any account can unlink it and put its own in place --
    a tombstone with revoked_at cleared, or a record whose digest it chose."""
    home = make_dir(tmp_path / "d", 0o777)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o600)
    assert os.stat(path).st_mode & 0o777 == 0o600  # the file half is clean
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(path, keys_module=keys)
    message = str(excinfo.value)
    assert str(home) in message
    assert "0777" in message


@POSIX_ONLY
def test_a_working_install_is_not_refused(installed):
    """0755 directory, 0600 file: what ~/.contextlake looks like on a machine with
    nothing wrong with it. This is the case that rejects a 0o077 parent mask, and
    it is why the fixture is 0755 and not 0700 (0700 & 0o077 == 0, so a widened
    mask would sail through)."""
    ring = keyfile.Keyring.load(installed["path"], keys_module=installed["keys"])
    assert ring.present is True
    assert len(ring) == 2


@POSIX_ONLY
def test_a_world_writable_parent_refuses_a_write(tmp_path):
    """The refusal fires on WRITE verbs too, and there is NO key file here.

    That combination is the one the serve path cannot cover: an absent key file
    means mint-and-start, exit 0, with no permission check at all. So the write
    verb is what stops a key file ever being born in a directory another account
    can write, and it refuses BEFORE creating anything."""
    home = make_dir(tmp_path / "d", 0o777)
    _, rec = record("A")
    with pytest.raises(keyfile.KeyFileError):
        keyfile.write_document(home / "mcp-keys.json", [rec])
    assert not (home / "mcp-keys.json").exists()


@POSIX_ONLY
def test_the_ancestors_it_creates_are_not_world_writable(tmp_path, monkeypatch):
    """Every directory this call creates, not only the last one.

    `Path.mkdir(parents=True)` applies the mode it is given to the LAST level and
    creates the ancestors at the default 0o777, so under a permissive umask the
    call whose stated job is refusing a world-writable parent was CREATING
    world-writable directories above the key file.

    The umask is set to 0 for the test, which is what makes the defect visible:
    at the developer's usual 0o022 the ancestors come out 0755, which carries no
    group or other WRITE bit and passes the check this test is about. A fixture at
    the ambient umask proves nothing.

    Three missing levels, because a fixture with one missing level has no
    ancestor to get wrong: `path.parent` is tightened afterwards whatever mkdir
    did with it, and that alone would hide this."""
    path = tmp_path / "a" / "b" / "c" / "mcp-keys.json"
    previous = os.umask(0)
    try:
        _, rec = record("A")
        keyfile.write_document(path, [rec])
    finally:
        os.umask(previous)

    for level in (tmp_path / "a", tmp_path / "a" / "b", path.parent):
        mode = os.stat(level).st_mode & 0o777
        assert mode == keyfile.PARENT_MODE, f"{level} is {mode:04o}"
        assert not mode & keyfile.PARENT_MASK, f"{level} is writable by others"
    assert os.stat(path).st_mode & 0o777 == 0o600


@POSIX_ONLY
def test_a_symlinked_parent_is_not_tightened_through_the_link(tmp_path):
    """`os.chmod(path.parent, 0o700)` follows a symlink and changes the mode of
    the TARGET, which is a directory the operator did not name here and which
    holds other things. The descriptor is opened O_NOFOLLOW instead, so a
    symlinked parent skips the tightening rather than locking down whatever is
    on the other end.

    Skipping is safe and the reason is in `_tighten_parent`: `enforce` already
    statted the parent THROUGH the link, so the target carries no group or other
    write bit and is owned by this account before a byte is written.

    The positive control is in the same body: a real directory still reaches
    0700, so this does not pass on an implementation that tightens nothing."""
    target = make_dir(tmp_path / "shared", 0o755)
    (target / "someone-elses-file").write_text("not ours\n")
    link = tmp_path / "linked-home"
    link.symlink_to(target)
    assert link.is_symlink()

    _, rec = record("A")
    keyfile.write_document(link / "mcp-keys.json", [rec])

    assert os.stat(target).st_mode & 0o777 == 0o755, (
        "the target of the symlink was tightened; that directory was not named here")
    assert (target / "mcp-keys.json").exists()
    assert os.stat(target / "mcp-keys.json").st_mode & 0o777 == 0o600

    # The positive control: a parent that is a real directory IS tightened.
    real_parent = make_dir(tmp_path / "real-home", 0o755)
    keyfile.write_document(real_parent / "mcp-keys.json", [rec])
    assert os.stat(real_parent).st_mode & 0o777 == keyfile.PARENT_MODE


@POSIX_ONLY
def test_a_world_writable_grandparent_is_not_checked(tmp_path, keys):
    """The documented boundary of the parent mask, pinned so the next reader
    finds the edge rather than assuming coverage.

    The check is the key file's own PARENT and no higher. An account that can
    write the GRANDPARENT can rename the parent away and put its own parent,
    holding its own key file, at that name -- and this loads without complaint.
    The residual is small because a world-writable ancestor in normal use is
    /tmp, and /tmp carries the sticky bit, which is what stops one account
    renaming another's entry inside it.

    The positive control is the same shape one level down: move the 0777 to the
    PARENT and it is refused, so this is not a test that passes on a module that
    checks nothing."""
    grand = make_dir(tmp_path / "grand", 0o777)
    parent = make_dir(grand / "keys", 0o700)
    _, rec = record("A")
    path = write_file(parent / "mcp-keys.json", document([rec]), 0o600)

    assert os.stat(grand).st_mode & keyfile.PARENT_MASK != 0
    assert keyfile.permission_report(path).faults == ()
    assert keyfile.Keyring.load(path, keys_module=keys).present is True

    os.chmod(parent, 0o777)
    assert len(keyfile.permission_report(path).faults) == 1
    with pytest.raises(keyfile.KeyFileError):
        keyfile.Keyring.load(path, keys_module=keys)


@POSIX_ONLY
def test_permission_report_never_refuses_and_names_every_failing_mask(tmp_path):
    """`kb keys list` must never refuse: blocking the admin from seeing what
    exists is the wrong failure, and it is the command they run to diagnose the
    refusal.

    Three cases. The both-at-once case is the one that matters: an implementation
    that prints the first fault it finds and stops passes the two single cases
    and hides the second fault, so the operator fixes one thing, re-runs, and is
    refused again."""
    _, rec = record("A")

    clean_dir = make_dir(tmp_path / "clean", 0o755)
    clean = write_file(clean_dir / "k.json", document([rec]), 0o600)
    assert keyfile.permission_report(clean).faults == ()

    loose_file_dir = make_dir(tmp_path / "loose-file", 0o755)
    loose_file = write_file(loose_file_dir / "k.json", document([rec]), 0o640)
    assert len(keyfile.permission_report(loose_file).faults) == 1

    loose_dir = make_dir(tmp_path / "loose-dir", 0o777)
    tight_file = write_file(loose_dir / "k.json", document([rec]), 0o600)
    assert len(keyfile.permission_report(tight_file).faults) == 1

    both_dir = make_dir(tmp_path / "both", 0o777)
    both = write_file(both_dir / "k.json", document([rec]), 0o640)
    faults = keyfile.permission_report(both).faults
    # Why it is 2 and not 4: the report also carries two OWNER checks, and both
    # pass here because tmp_path belongs to the account running the test. Said
    # out loud so the count does not silently depend on who owns tmp_path.
    assert len(faults) == 2
    assert [line for line in faults if "owned by uid" in line] == []


def test_the_permission_checks_are_skipped_off_posix(tmp_path, monkeypatch):
    """POSIX only, asserted in BOTH directions.

    os.chmod on Windows sets only the read-only flag, so a mode check that
    silently passed there would read as protection it does not provide. It says
    it was skipped instead."""
    home = make_dir(tmp_path / "d", 0o777)
    _, rec = record("A")
    path = write_file(home / "k.json", document([rec]), 0o640)

    on = keyfile.permission_report(path)
    assert len(on.faults) == 2 and on.skipped is None

    monkeypatch.setattr(keyfile, "POSIX", False)
    off = keyfile.permission_report(path)
    assert off.faults == ()
    assert off.skipped == keyfile.PERMISSION_CHECK_SKIPPED
    assert "SKIPPED" in off.skipped
    keyfile.enforce(path)  # and it does not raise


# ==========================================================================
# S4.2.3 -- the file is not touched until a keyring is built
# ==========================================================================


@POSIX_ONLY
def test_importing_the_module_touches_no_file_and_loading_touches_two(installed):
    """Criterion 11's two halves in one test.

    The 0-call half alone passes on an accessor nothing ever calls, so the
    positive control is the second half: building a Keyring stats twice (the key
    file and its parent) and reads once."""
    fresh = importlib.reload(keyfile)
    try:
        assert fresh.counters() == {"stat": 0, "read": 0}
        trace: list[tuple[str, str]] = []
        fresh.reset_counters(trace)
        fresh.Keyring.load(installed["path"], keys_module=installed["keys"])
        assert fresh.counters() == {"stat": 2, "read": 1}
        assert trace == [
            ("stat", str(installed["path"])),
            ("stat", str(installed["dir"])),
            ("read", str(installed["path"])),
        ]
    finally:
        importlib.reload(keyfile)


# ==========================================================================
# S4.2.4 -- the lookup
# ==========================================================================


class CountingMap(dict):
    """A digest map that reports every whole-keyring traversal.

    Counting `.get` alone cannot fail: a linear scan reads records without one.
    The traversal counter is what a scan trips."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.traversals = 0
        self.gets = 0

    def __iter__(self):
        self.traversals += 1
        return super().__iter__()

    def keys(self):
        self.traversals += 1
        return super().keys()

    def values(self):
        self.traversals += 1
        return super().values()

    def items(self):
        self.traversals += 1
        return super().items()

    def get(self, key, default=None):
        self.gets += 1
        return super().get(key, default)


def big_keyring(tmp_path, keys, *, wanted_tag="LAST", count=1000):
    """A 1,000-record keyring with the resolved key inserted LAST.

    Insertion order is load-bearing. A linear scan that stops at the match
    touches ONE record when the resolved key sits first, which is what a fixture
    that creates one key and pads afterwards naturally produces -- and it is how
    the linear-scan break-test passed against the earlier single-case criterion.
    """
    records = [record(f"P{i}")[1] for i in range(count - 1)]
    wanted_key, wanted = record(wanted_tag)
    records.append(wanted)
    home = make_dir(tmp_path / "d", 0o755)
    path = write_file(home / "mcp-keys.json", document(records), 0o600)
    ring = keyfile.Keyring.load(path, keys_module=keys)
    counting = CountingMap(ring._by_digest)
    ring._by_digest = counting
    keys.touched.clear()
    return ring, counting, wanted_key


def test_resolve_touches_one_record_not_the_whole_keyring(tmp_path, keys):
    ring, counting, wanted_key = big_keyring(tmp_path, keys)

    found = ring.resolve(wanted_key)

    assert found is not None
    assert found[0].id == "k_LAST"
    assert counting.traversals == 0
    assert counting.gets == 1
    assert set(keys.touched) == {"k_LAST"}


def test_an_unknown_digest_does_not_scan_the_keyring(tmp_path, keys):
    """An unknown but well-formed value forces a full scan unconditionally, so
    this case cannot pass by insertion position the way the case above can."""
    ring, counting, _ = big_keyring(tmp_path, keys)

    assert ring.resolve(fake_key("NOSUCH")) is None

    assert counting.traversals == 0
    assert counting.gets == 1
    assert keys.touched == []


def test_resolve_makes_no_constant_time_comparison_for_a_ctxlake_key(installed,
                                                                     monkeypatch):
    """Two assertions, because either alone is weak.

    The AST scan proves the module names no `hmac.compare_digest` call (prose in
    a docstring does not trip it). The live counter proves the lookup path makes
    none at run time -- and the counter is proved awake first, so it is not an
    inert probe scoring 0 because nothing installed it."""
    tree = ast.parse(Path(keyfile.__file__).read_text(encoding="utf-8"))
    named = [n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "compare_digest"]
    assert named == []
    assert "hmac" not in {n.name for n in ast.walk(tree)
                          if isinstance(n, ast.alias)}

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest",
                        lambda a, b: (calls.append(1), real(a, b))[1])
    assert hmac.compare_digest(b"x", b"x") is True  # the counter is awake
    assert len(calls) == 1

    ring = keyfile.Keyring.load(installed["path"], keys_module=installed["keys"])
    assert ring.resolve(installed["key_a"]) is not None
    assert len(calls) == 1


def test_resolve_returns_the_class_for_a_revoked_and_an_expired_key(tmp_path, keys):
    """A revoked or expired key comes back WITH its class, not as None, so the
    caller records the refusal class without a second lookup."""
    home = make_dir(tmp_path / "d", 0o755)
    live_key, live = record("LIVE")
    revoked_key, revoked = record("GONE", revoked_at="2026-02-01T00:00:00Z")
    expired_key, expired = record("OLD", expires_at="2026-01-02T00:00:00Z")
    path = write_file(home / "mcp-keys.json",
                      document([live, revoked, expired]), 0o600)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    ring = keyfile.Keyring.load(path, now=lambda: now, keys_module=keys)

    assert ring.resolve(live_key)[1] == "live"
    assert ring.resolve(revoked_key)[1] == "revoked"
    assert ring.resolve(expired_key)[1] == "expired"
    assert ring.resolve(fake_key("NOSUCH")) is None
    assert ring.live_count() == 1


def test_resolve_refuses_a_non_ascii_bearer_value_without_raising(installed):
    """The gate hands over the raw header bytes. A hostile value carrying
    non-ASCII must come back as None so the wire sees a 401; letting a
    UnicodeDecodeError escape would surface it as a 500 instead."""
    ring = keyfile.Keyring.load(installed["path"], keys_module=installed["keys"])
    assert ring.resolve(b"\xff\xfe\x00bearer") is None
    assert ring.resolve("ctxläke_" + "x" * 49) is None
    assert ring.resolve(None) is None
    assert ring.resolve(installed["key_a"].encode("ascii")) is not None


# ==========================================================================
# S4.2.4 -- freshness
# ==========================================================================


def stamp_of(path: Path):
    st = os.stat(path)
    return st.st_ino, st.st_size, st.st_mtime_ns


def test_two_writes_in_one_mtime_tick_are_both_seen(tmp_path, keys):
    """The st_ino case, which is the one that bites.

    This module's own writer is temp-sibling-plus-os.replace, so every write
    lands a NEW INODE on the path. Two writes inside one coarse mtime tick differ
    only in the inode, and an mtime-only check would keep serving a revoked key
    until the clock ticked. The test pins the size and the mtime EQUAL so the
    inode is the only field that moved -- without those two assertions it passes
    on a size change and never exercises the inode at all."""
    home = make_dir(tmp_path / "d", 0o755)
    key_a, rec_a = record("A")
    _, revoked = record("A", revoked_at="2026-02-01T00:00:00Z")
    first, second = document([rec_a]), document([revoked])
    width = max(len(first), len(second))  # JSON ignores trailing whitespace
    first += b" " * (width - len(first))
    second += b" " * (width - len(second))
    assert len(second) == len(first)
    path = write_file(home / "mcp-keys.json", first, 0o600)
    ring = keyfile.Keyring.load(path, keys_module=keys)
    assert ring.resolve(key_a)[1] == "live"

    before = stamp_of(path)
    atomic_replace(path, second)
    os.utime(path, ns=(before[2], before[2]))

    after = stamp_of(path)
    assert after[0] != before[0], "the inode did not move; the fixture proves nothing"
    assert after[1] == before[1], "the size moved; the inode is not being tested"
    assert after[2] == before[2], "the mtime moved; the inode is not being tested"

    assert ring.reload_if_changed() is True
    assert ring.resolve(key_a)[1] == "revoked"


def test_a_size_change_alone_triggers_a_reload(tmp_path, keys):
    home = make_dir(tmp_path / "d", 0o755)
    key_a, rec_a = record("A")
    path = write_file(home / "mcp-keys.json", document([rec_a]), 0o600)
    ring = keyfile.Keyring.load(path, keys_module=keys)
    before = stamp_of(path)

    _, rec_b = record("B")
    with open(path, "wb") as fh:  # in place, so the inode does NOT move
        fh.write(document([rec_a, rec_b]))
    os.utime(path, ns=(before[2], before[2]))

    after = stamp_of(path)
    assert after[0] == before[0] and after[2] == before[2] and after[1] != before[1]
    assert ring.reload_if_changed() is True
    assert len(ring) == 2


def test_an_mtime_change_alone_triggers_a_reload(tmp_path, keys):
    home = make_dir(tmp_path / "d", 0o755)
    key_a, rec_a = record("A")
    payload = document([rec_a])
    path = write_file(home / "mcp-keys.json", payload, 0o600)
    ring = keyfile.Keyring.load(path, keys_module=keys)
    before = stamp_of(path)

    with open(path, "wb") as fh:  # identical bytes, same inode
        fh.write(payload)
    os.utime(path, ns=(before[2] + 1_000_000_000, before[2] + 1_000_000_000))

    after = stamp_of(path)
    assert after[0] == before[0] and after[1] == before[1] and after[2] != before[2]
    assert ring.reload_if_changed() is True


def test_an_unchanged_file_is_read_zero_times(installed, counted):
    """One stat per request, ON THE KEY FILE, and no read at all.

    The path is pinned, not only the count: a stat on the parent directory also
    satisfies `count == 100`."""
    ring = keyfile.Keyring.load(installed["path"], keys_module=installed["keys"])
    keyfile.reset_counters(counted)
    del counted[:]

    reloaded = 0
    for _ in range(100):
        reloaded += ring.reload_if_changed()
        assert ring.resolve(installed["key_a"])[1] == "live"

    # The counters first, so a module that re-reads fails on the NUMBER the
    # ticket names rather than on a boolean one iteration in.
    assert keyfile.counters() == {"stat": 100, "read": 0}
    assert reloaded == 0
    assert set(counted) == {("stat", str(installed["path"]))}


def test_a_broken_reload_keeps_the_last_good_snapshot(installed, keys):
    """Substituting an empty keyring admits nobody. This is the half that catches
    it."""
    ring = keyfile.Keyring.load(installed["path"], keys_module=keys)
    assert ring.resolve(installed["key_a"])[1] == "live"

    atomic_replace(installed["path"], b"{ truncated mid-write")

    assert ring.reload_if_changed() is False
    found = ring.resolve(installed["key_a"])
    assert found is not None, "an empty keyring was substituted; it admits nobody"
    assert found[1] == "live"


def test_a_broken_reload_does_not_admit_an_unknown_key(installed, keys):
    """Skipping the check admits everybody. This is the half that catches THAT,
    and failing open passes the test above while failing this one."""
    ring = keyfile.Keyring.load(installed["path"], keys_module=keys)
    atomic_replace(installed["path"], b"{ truncated mid-write")

    assert ring.reload_if_changed() is False
    assert ring.resolve(fake_key("NOSUCH")) is None


def test_a_broken_reload_warns_once_not_a_hundred_times(installed, keys):
    """Warn on the transition into failure, not per request -- and warn AGAIN
    after a recovery. "Warn once ever" passes the first half and then goes
    silent through the next outage."""
    said: list[str] = []
    ring = keyfile.Keyring.load(installed["path"], warn=said.append, keys_module=keys)
    atomic_replace(installed["path"], b"{ truncated mid-write")

    for _ in range(100):
        ring.reload_if_changed()
    assert len(said) == 1
    assert str(installed["path"]) in said[0]

    atomic_replace(installed["path"], document([installed["rec_a"]]))
    assert ring.reload_if_changed() is True
    assert len(said) == 1

    atomic_replace(installed["path"], b"{ broken again")
    for _ in range(100):
        ring.reload_if_changed()
    assert len(said) == 2


@POSIX_ONLY
def test_a_file_widened_after_start_stops_being_trusted(installed, keys):
    """The file mask is re-read on reload, off the stat the freshness triple
    already takes, so it costs no extra syscall.

    It is re-read through `permission_report`, the load path's own
    implementation, and so are the owner check and the parent mask -- see the
    three tests below. What stays cheap is the UNCHANGED file: that path returns
    on the stamp before any permission work, so the per-request cost is still one
    stat on the key file and nothing else."""
    said: list[str] = []
    ring = keyfile.Keyring.load(installed["path"], warn=said.append, keys_module=keys)
    _, rec_c = record("C")
    atomic_replace(installed["path"],
                   document([installed["rec_a"], installed["rec_b"], rec_c]))
    os.chmod(installed["path"], 0o644)

    assert ring.reload_if_changed() is False
    assert len(ring) == 2  # the widened file's third key was NOT taken
    assert len(said) == 1 and "0644" in said[0]


def test_the_reload_path_refuses_through_permission_report(installed, keys,
                                                           monkeypatch):
    """The twin of `test_the_load_path_refuses_through_permission_report`, and
    the assertion that there is ONE implementation rather than two.

    `reload_if_changed` open-coded the FILE_MASK check for a round while
    `Keyring.load` went through `permission_report`, so the reload path saw a
    widened file and NOT a changed owner and NOT a widened parent, and a running
    server adopted a key file the load path would have refused.

    Asserted at the seam, not by comparing two outputs: `permission_report` is
    replaced with one that reports a fault no mask produces, and BOTH paths have
    to refuse with that text. Gutting `_file_fault` instead is a weaker probe --
    a pass there cannot tell "shared implementation" from "two copies that still
    happen to agree"."""
    said: list[str] = []
    ring = keyfile.Keyring.load(installed["path"], warn=said.append, keys_module=keys)
    assert ring.resolve(installed["key_a"])[1] == "live"

    monkeypatch.setattr(
        keyfile, "permission_report",
        lambda p, **kw: keyfile.PermissionReport(faults=("SENTINEL-FAULT",)))
    _, rec_c = record("C")
    atomic_replace(installed["path"],
                   document([installed["rec_a"], installed["rec_b"], rec_c]))

    assert ring.reload_if_changed() is False
    assert len(ring) == 2, "the third key was taken through a refused report"
    assert len(said) == 1 and "SENTINEL-FAULT" in said[0]


@POSIX_ONLY
def test_a_reload_refuses_a_file_whose_owner_changed(installed, keys, monkeypatch):
    """The owner check runs on the reload path, not only at start.

    Nothing in the file's MODE moves when it changes hands, so the mask the
    reload path used to open-code cannot see this at all. The owner may write
    the file whatever the mode says, and may set the mode back afterwards.

    `os.geteuid` is monkeypatched rather than the file chown-ed: an unprivileged
    test cannot give a file away, and a test that needs root does not run. The
    load first, THEN the change, so the keyring has a good snapshot to keep."""
    said: list[str] = []
    ring = keyfile.Keyring.load(installed["path"], warn=said.append, keys_module=keys)
    assert ring.resolve(installed["key_a"])[1] == "live"

    stranger = os.stat(installed["path"]).st_uid + 1
    monkeypatch.setattr(os, "geteuid", lambda: stranger)
    _, rec_c = record("C")
    atomic_replace(installed["path"],
                   document([installed["rec_a"], installed["rec_b"], rec_c]))

    assert ring.reload_if_changed() is False
    assert len(ring) == 2, "a file owned by another account was adopted"
    assert ring.resolve(installed["key_a"])[1] == "live", "the snapshot was dropped"
    assert len(said) == 1 and "owned by uid" in said[0]


@POSIX_ONLY
def test_a_reload_refuses_a_parent_widened_after_start(installed, keys):
    """The parent mask runs on the reload path when the file has changed.

    The file itself stays 0600 and perfect. Its directory goes 0777, so any
    account can unlink the key file and put its own in place, and the substituted
    file is what the reload would adopt. A file-mask-only reload sees nothing
    wrong with it.

    Cheaper on an UNCHANGED file, deliberately: that path returns on the stamp
    before any permission work, so a parent widened while the key file is not
    touched is not seen until the next change to the file. Narrow, because the
    write bit is wanted in order to REPLACE the file, and replacing it moves the
    freshness triple."""
    said: list[str] = []
    ring = keyfile.Keyring.load(installed["path"], warn=said.append, keys_module=keys)

    os.chmod(installed["dir"], 0o777)
    _, rec_c = record("C")
    atomic_replace(installed["path"],
                   document([installed["rec_a"], installed["rec_b"], rec_c]))
    assert os.stat(installed["path"]).st_mode & 0o777 == 0o600, (
        "the file half moved; this is not testing the parent")

    assert ring.reload_if_changed() is False
    assert len(ring) == 2, "the substituted file's third key was taken"
    assert len(said) == 1 and "0777" in said[0]


# ==========================================================================
# S4.2.4 -- the clock
# ==========================================================================


def test_the_keyring_clock_is_wall_clock_not_monotonic(installed, keys):
    """Three assertions. The name scan alone is a lint; the value assertion is
    what catches a monotonic clock dressed up as a datetime, which is the shape
    an implementer actually writes."""
    # The VALUE assertion first. time.monotonic() is seconds since boot on Linux,
    # so a monotonic clock dressed as a datetime reads a 1970 date, every
    # expires_at sits in its future, and no key ever lapses.
    default = keyfile._default_now()
    assert default.tzinfo is not None
    assert abs(default - datetime.now(timezone.utc)) < timedelta(days=1)

    # Then the name scan, which catches a monotonic clock reached some other way.
    # AST rather than a substring search: this module explains in prose WHY it
    # does not use time.monotonic, and a text scan would trip on the explanation.
    tree = ast.parse(Path(keyfile.__file__).read_text(encoding="utf-8"))
    assert [n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "monotonic"] == []

    home = installed["dir"]
    key, rec = record("EXP", expires_at="2026-06-01T00:00:00Z")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o600)
    clock = {"now": datetime(2026, 5, 1, tzinfo=timezone.utc)}
    ring = keyfile.Keyring.load(path, now=lambda: clock["now"], keys_module=keys)
    assert ring.resolve(key)[1] == "live"

    clock["now"] = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert ring.resolve(key)[1] == "expired"  # no file change, no reload


# ==========================================================================
# the seam to contextlake.kb.keys
# ==========================================================================


def test_the_keys_module_contract_matches_the_real_module():
    """The names keyfile asks for are the names keys.py actually has.

    Pinned as VALUES, not read symbolically: a contract tuple every caller reads
    through the constant can be changed to anything without a failure."""
    from contextlake.kb import keys as real

    assert keyfile.KEYS_MODULE_CONTRACT == ("check_format", "digest", "KeyRecord")
    for name in keyfile.KEYS_MODULE_CONTRACT:
        assert hasattr(real, name), name
    assert hasattr(real.KeyRecord, "from_dict")
    assert hasattr(real.KeyRecord, "to_dict")


def test_the_real_keys_module_round_trips_through_the_file(tmp_path):
    """Write, load and resolve with NO keys_module= injection.

    The key is built from a fixed, public byte pattern with `key_from_bytes`,
    never `mint()`: nothing here draws a secret, and the value is reproducible
    from this line alone.
    """
    from contextlake.kb import keys as real

    raw = b"NOT-A-REAL-KEY-fixture-bytes-000"[:32].ljust(32, b"0")
    key = real.key_from_bytes(raw)
    assert real.check_format(key)
    rec = real.KeyRecord(id="k_aaa111", name="fixture", digest=real.digest(key),
                         created_at="2026-01-01T00:00:00Z")

    path = tmp_path / "d" / "mcp-keys.json"
    keyfile.write_document(path, [rec])
    assert key.encode("ascii") not in path.read_bytes()

    ring = keyfile.Keyring.load(path)
    found = ring.resolve(key.encode("ascii"))
    assert found is not None
    assert found[0].id == "k_aaa111"
    assert found[1] == "live"
    assert ring.resolve(real.key_from_bytes(b"1" * 32)) is None


def test_a_keys_module_missing_the_contract_names_what_is_missing(installed):
    class Partial:
        __name__ = "partial"

        def check_format(self, value):
            return True

    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(installed["path"], keys_module=Partial())
    message = str(excinfo.value)
    assert "digest" in message and "KeyRecord" in message


# ==========================================================================
# THE PATH RULE
#
# One `os.lstat`, one directory entry, and the states that are neither
# "absent" nor "present and fine". Three round-1 blockers came from three
# functions each resolving the path their own way, so these pin the RULE
# rather than the three call sites.
# ==========================================================================


def symlink_to(link: Path, target) -> Path:
    """A symlink at `link`. Created under tmp_path only, never anywhere real."""
    link.symlink_to(target)
    assert link.is_symlink()
    return link


def test_a_dangling_symlink_is_present_and_refused_not_absent(tmp_path, keys):
    """`Path.exists()` answers False for a broken link, and False meant "no key
    file", which meant "mint one unscoped shared token". A broken link is a
    PRESENT path this reader cannot use, and it is refused.

    The positive control is in the same test: replace the link with a real file
    in the same directory and it loads, so this is not a check that refuses
    everything."""
    home = make_dir(tmp_path / "d", 0o755)
    link = symlink_to(home / "mcp-keys.json", tmp_path / "never-created.json")

    assert link.exists() is False, "the fixture does not reproduce the swallow"
    state = keyfile.inspect_key_file(link)
    assert state.kind == keyfile.SYMLINK
    assert state.usable is False

    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(link, keys_module=keys)
    assert "symlink" in str(excinfo.value)

    link.unlink()
    _, rec = record("A")
    real = write_file(home / "mcp-keys.json", document([rec]), 0o600)
    assert keyfile.Keyring.load(real, keys_module=keys).present is True


@POSIX_ONLY
def test_a_symlinked_key_file_cannot_split_the_two_masks(tmp_path, keys):
    """The file mode was read THROUGH the link while the parent was taken
    LEXICALLY, so the two masks described two different directories.

    The fixture is the bypass. The link sits in a 0700 directory, so the parent
    mask passes. It points at a 0600 file, so the file mask passes. That file
    lives in a 0777 directory no check ever looked at, and any account which can
    write there replaces it wholesale. Both masks green, key file replaceable by
    anyone. The three asserts before the raise prove the fixture really is that
    shape rather than being refused for some other reason."""
    tight = make_dir(tmp_path / "tight", 0o700)
    loose = make_dir(tmp_path / "loose", 0o777)
    _, rec = record("A")
    target = write_file(loose / "real.json", document([rec]), 0o600)
    link = symlink_to(tight / "mcp-keys.json", target)

    assert os.stat(link).st_mode & keyfile.FILE_MASK == 0
    assert os.stat(link.parent).st_mode & keyfile.PARENT_MASK == 0
    assert os.stat(target.parent).st_mode & keyfile.PARENT_MASK != 0

    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(link, keys_module=keys)
    assert "symlink" in str(excinfo.value)
    assert keyfile.permission_report(link).faults != ()


def test_a_directory_at_the_key_file_path_is_refused(tmp_path, keys):
    """Absent and regular are not the only two shapes a path can have."""
    home = make_dir(tmp_path / "d", 0o755)
    made = make_dir(home / "mcp-keys.json", 0o700)

    assert keyfile.inspect_key_file(made).kind == keyfile.IRREGULAR
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(made, keys_module=keys)
    assert "not a regular file" in str(excinfo.value)


@POSIX_ONLY
@pytest.mark.skipif(os.geteuid() == 0, reason="root can stat anything")
def test_a_key_file_that_cannot_be_stat_ed_is_not_absent(tmp_path, keys):
    """The second door to the same downgrade: `Path.exists()` swallows
    PermissionError and answers False, and False meant mint."""
    home = make_dir(tmp_path / "d", 0o755)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o600)
    os.chmod(home, 0o000)
    try:
        assert path.exists() is False, "the fixture does not reproduce the swallow"
        state = keyfile.inspect_key_file(path)
        assert state.kind == keyfile.UNSTATTABLE
        with pytest.raises(keyfile.KeyFileError) as excinfo:
            keyfile.Keyring.load(path, keys_module=keys)
        assert "cannot be examined" in str(excinfo.value)
    finally:
        os.chmod(home, 0o755)
    # Same file, same account, readable directory: it loads.
    assert keyfile.Keyring.load(path, keys_module=keys).present is True


@POSIX_ONLY
def test_an_entry_owned_by_another_account_is_refused(tmp_path, keys, monkeypatch):
    """Neither mask asks who OWNS the entry. The owner may write it whatever the
    mode says, and may set the mode back afterwards, so 0600 protects nothing
    against the account that owns it.

    `os.geteuid` is monkeypatched rather than the file chown-ed: an unprivileged
    test cannot give a file away, and a test that needs root does not run."""
    home = make_dir(tmp_path / "d", 0o755)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o600)
    assert keyfile.permission_report(path).faults == ()

    stranger = os.stat(path).st_uid + 1
    monkeypatch.setattr(os, "geteuid", lambda: stranger)
    faults = keyfile.permission_report(path).faults
    assert len(faults) == 2, faults          # the file AND its directory
    assert all("owned by uid" in line for line in faults)
    with pytest.raises(keyfile.KeyFileError):
        keyfile.Keyring.load(path, keys_module=keys)

    # Root passes: root can already write every file on this machine, so
    # refusing a root-owned key file reports a risk that admitting it does not
    # add. Asserted on the check itself -- an unprivileged test cannot create a
    # root-owned file, and faking the stat of one and not the other reports the
    # wrong thing.
    assert keyfile._owner_fault("the key file", path, 0) is None
    assert keyfile._owner_fault("the key file", path, stranger) is None
    assert keyfile._owner_fault("the key file", path, stranger + 1) is not None


def test_the_load_path_refuses_through_permission_report(tmp_path, keys,
                                                         monkeypatch):
    """One implementation, one caller.

    `Keyring.load` carried its OWN copy of the mask logic, so gutting
    `permission_report` left both load-path refusal tests green: a change to one
    copy did not reach the other. Asserted at the seam rather than by comparing
    two outputs -- `permission_report` is replaced with one that reports a fault
    no mask produces, and the load path has to refuse with that text."""
    home = make_dir(tmp_path / "d", 0o755)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o600)
    assert keyfile.Keyring.load(path, keys_module=keys).present is True

    monkeypatch.setattr(
        keyfile, "permission_report",
        lambda p, **kw: keyfile.PermissionReport(faults=("SENTINEL-FAULT",)))
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(path, keys_module=keys)
    assert "SENTINEL-FAULT" in str(excinfo.value)


def test_the_stamp_describes_the_bytes_that_were_read(tmp_path, keys, monkeypatch):
    """The freshness triple comes from the `fstat` of the descriptor the BYTES
    came from, never from the earlier lstat.

    The race is this module's own writer: every write renames a new inode onto
    the path, so a stamp taken before the open can describe an inode the records
    did not come from. The next `reload_if_changed` then matches that stale
    stamp and serves the stale table for the life of the process, silently."""
    home = make_dir(tmp_path / "d", 0o755)
    key_a, rec_a = record("A")
    _, rec_b = record("B")
    path = write_file(home / "mcp-keys.json", document([rec_a]), 0o600)

    real_inspect = keyfile.inspect_key_file
    raced: list[int] = []

    def racing(candidate):
        state = real_inspect(candidate)
        if not raced:
            raced.append(1)
            atomic_replace(path, document([rec_a, rec_b]))
        return state

    monkeypatch.setattr(keyfile, "inspect_key_file", racing)
    ring = keyfile.Keyring.load(path, keys_module=keys)
    monkeypatch.setattr(keyfile, "inspect_key_file", real_inspect)

    assert raced == [1], "the race never fired; the fixture proves nothing"
    assert len(ring) == 2, "the bytes read were the ones the race wrote"
    keyfile.reset_counters()
    try:
        assert ring.reload_if_changed() is False, (
            "the stored stamp describes an inode the records did not come from")
        assert keyfile.counters()["read"] == 0
    finally:
        keyfile.reset_counters()
    assert ring.resolve(key_a)[1] == "live"


@POSIX_ONLY
def test_a_symlink_swapped_in_after_the_check_is_not_followed(tmp_path, keys,
                                                              monkeypatch):
    """O_NOFOLLOW on the read.

    Classifying the path and opening it are two syscalls, so a symlink can land
    between them. The open refuses it instead of reading whatever it points at,
    and the refusal is a KeyFileError, so the caller keeps its snapshot rather
    than treating the file as absent."""
    home = make_dir(tmp_path / "d", 0o755)
    _, rec = record("A")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o600)
    _, other = record("Z")
    elsewhere = write_file(tmp_path / "elsewhere.json", document([other]), 0o600)

    real_inspect = keyfile.inspect_key_file
    raced: list[int] = []

    def racing(candidate):
        state = real_inspect(candidate)
        if not raced:
            raced.append(1)
            path.unlink()
            symlink_to(path, elsewhere)
        return state

    monkeypatch.setattr(keyfile, "inspect_key_file", racing)
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.Keyring.load(path, keys_module=keys)
    assert raced == [1], "the race never fired; the fixture proves nothing"
    assert "cannot read the key file" in str(excinfo.value)


# ==========================================================================
# THE WRITE -- the temp sibling, and durability
# ==========================================================================


@POSIX_ONLY
def test_a_pre_created_temp_sibling_cannot_take_over_the_key_file(tmp_path):
    """The temp name used to be fixed at `<name>.tmp`, opened with O_CREAT and
    no O_EXCL, so a pre-existing file kept its OWN mode and the key file landed
    0666 in a reviewer's run.

    The obstacle is pre-created at the exact old name. It must still be sitting
    there, byte for byte, when the write finishes."""
    home = make_dir(tmp_path / "d", 0o700)
    obstacle = home / "mcp-keys.json.tmp"
    obstacle.write_text("squatted")
    os.chmod(obstacle, 0o666)

    _, rec = record("A")
    keyfile.write_document(home / "mcp-keys.json", [rec])

    assert os.stat(home / "mcp-keys.json").st_mode & 0o777 == 0o600
    assert obstacle.read_text() == "squatted"
    assert os.stat(obstacle).st_mode & 0o777 == 0o666


@POSIX_ONLY
def test_a_pre_created_temp_symlink_cannot_clobber_its_target(tmp_path):
    """A `<name>.tmp` SYMLINK was followed: the payload landed in whatever it
    pointed at, and `os.replace` then left the KEY FILE as a symlink."""
    home = make_dir(tmp_path / "d", 0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite me")
    obstacle = symlink_to(home / "mcp-keys.json.tmp", victim)

    _, rec = record("A")
    keyfile.write_document(home / "mcp-keys.json", [rec])

    assert victim.read_text() == "do not overwrite me"
    assert (home / "mcp-keys.json").is_symlink() is False
    assert os.stat(home / "mcp-keys.json").st_mode & 0o777 == 0o600
    assert obstacle.is_symlink()


def test_the_bytes_are_fsynced_before_the_rename(tmp_path, monkeypatch):
    """Durability is not observable in-process, so the ORDER is what is asserted.

    A rename made durable ahead of the data leaves a key file of zeros that
    parses as nothing. Order and not presence: an fsync AFTER the rename passes
    a presence check and fixes nothing."""
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace",
                        lambda src, dst: (calls.append("replace"),
                                          real_replace(src, dst))[1])

    _, rec = record("A")
    keyfile.write_document(tmp_path / "d" / "mcp-keys.json", [rec])

    assert "replace" in calls, "the writer did not rename at all"
    assert calls.index("fsync") < calls.index("replace"), calls


@POSIX_ONLY
def test_the_write_refuses_a_symlink_at_the_key_file_path(tmp_path):
    """The write verbs go through the same path rule as the read.

    Without this, `kb keys create` pointed at a symlink writes through it and
    the key file stays a link into a directory no mask ever checks."""
    home = make_dir(tmp_path / "d", 0o700)
    victim = write_file(tmp_path / "victim.json", b"{}\n", 0o600)
    link = symlink_to(home / "mcp-keys.json", victim)

    _, rec = record("A")
    with pytest.raises(keyfile.KeyFileError) as excinfo:
        keyfile.write_document(link, [rec])
    assert "symlink" in str(excinfo.value)
    assert victim.read_bytes() == b"{}\n"


# ==========================================================================
# RELOAD -- deletion, and the rollback ratchet
# ==========================================================================


def test_a_deleted_key_file_keeps_the_last_good_snapshot_and_warns(installed, keys):
    """The docstring promised "never an empty keyring" and the code delivered
    one through the SUCCESS path, so the failure branch never ran: every client
    was locked out and the operator got no signal at all.

    Three assertions, and each catches it: the return was True, the map was
    emptied, and nothing warned."""
    said: list[str] = []
    ring = keyfile.Keyring.load(installed["path"], warn=said.append, keys_module=keys)
    assert ring.resolve(installed["key_a"])[1] == "live"

    installed["path"].unlink()

    assert ring.reload_if_changed() is False
    assert len(ring) == 2, "an empty keyring was substituted; it admits nobody"
    found = ring.resolve(installed["key_a"])
    assert found is not None and found[1] == "live"
    assert len(said) == 1 and "DELETED" in said[0]

    for _ in range(100):
        assert ring.reload_if_changed() is False
    assert len(said) == 1, "warn once per failure transition, not per request"


def test_a_keyring_born_without_a_file_does_not_warn_about_one(tmp_path, keys):
    """The other direction, so deletion-is-a-failure is not "absent always
    fails". A path that never had a file is the state that lets the caller mint,
    and a file appearing later is picked up."""
    said: list[str] = []
    path = tmp_path / "d" / "mcp-keys.json"
    ring = keyfile.Keyring.load(path, warn=said.append, keys_module=keys)
    assert ring.present is False
    for _ in range(10):
        assert ring.reload_if_changed() is False
    assert said == []

    _, rec = record("A")
    write_file(make_dir(tmp_path / "d", 0o755) / "mcp-keys.json",
               document([rec]), 0o600)
    assert ring.reload_if_changed() is True
    assert ring.present is True and len(ring) == 1
    assert said == []


def test_a_rollback_cannot_revive_a_revoked_key(tmp_path, keys):
    """The schema gate refuses a NEWER file. It cannot see an OLDER COPY at the
    same version, and restoring yesterday's file un-revokes every key revoked
    since.

    Live-process half only. Across a restart the reader takes whatever is on
    disk, and the controls there are the parent mask and the owner check. The
    last block is the other direction: a legitimate later write is still taken,
    so this is not a ratchet that freezes the file."""
    home = make_dir(tmp_path / "d", 0o755)
    key_a, live = record("A")
    _, revoked = record("A", revoked_at="2026-02-01T00:00:00Z")
    yesterday = document([live])
    said: list[str] = []
    path = write_file(home / "mcp-keys.json", yesterday, 0o600)
    ring = keyfile.Keyring.load(path, warn=said.append, keys_module=keys)
    assert ring.resolve(key_a)[1] == "live"

    atomic_replace(path, document([revoked]))
    assert ring.reload_if_changed() is True
    assert ring.resolve(key_a)[1] == "revoked"

    atomic_replace(path, yesterday)
    assert ring.reload_if_changed() is False
    assert ring.resolve(key_a)[1] == "revoked", "the rollback revived a revoked key"
    assert len(said) == 1 and "REVOKED" in said[0]

    _, rec_b = record("B")
    atomic_replace(path, document([revoked, rec_b]))
    assert ring.reload_if_changed() is True
    assert len(ring) == 2
    assert ring.resolve(key_a)[1] == "revoked"


def test_a_key_dropped_from_the_file_is_not_a_revival(tmp_path, keys):
    """A digest that DISAPPEARED cannot authenticate, so removing a tombstone is
    a prune and not a rollback. Without this the ratchet would refuse every
    `kb keys prune`."""
    home = make_dir(tmp_path / "d", 0o755)
    key_a, _live = record("A")
    _, revoked = record("A", revoked_at="2026-02-01T00:00:00Z")
    _, rec_b = record("B")
    path = write_file(home / "mcp-keys.json", document([revoked, rec_b]), 0o600)
    ring = keyfile.Keyring.load(path, keys_module=keys)
    assert ring.resolve(key_a)[1] == "revoked"

    atomic_replace(path, document([rec_b]))
    assert ring.reload_if_changed() is True
    assert ring.resolve(key_a) is None
    assert len(ring) == 1


def test_a_refused_path_is_stat_ed_once_and_read_zero_times(tmp_path, keys,
                                                            counted):
    """The rule this whole change is: read the key-file path ONCE, the same way.

    Asserted as a COUNT, and every other test here asserts a DECISION. Round 1
    asked this exact path twice -- once through `Path.exists()` and once inside
    the load -- and the two answers disagreed, which is how a dangling symlink
    became "no key file" and then a minted shared token. A second stat that
    happens to agree with the first passes every decision test in this file.

    The parent is statted too, and that is a different path and a load-time
    cost. What is pinned is the KEY FILE path: exactly one touch, and no read.
    """
    home = make_dir(tmp_path / "d", 0o755)
    link = symlink_to(home / "mcp-keys.json", tmp_path / "never-created.json")
    keyfile.reset_counters(counted)
    del counted[:]

    with pytest.raises(keyfile.KeyFileError):
        keyfile.Keyring.load(link, keys_module=keys)

    assert [entry for entry in counted if entry[1] == str(link)] == [
        ("stat", str(link))]
    assert keyfile.counters()["read"] == 0
    assert {entry[1] for entry in counted} == {str(link), str(home)}


# ==========================================================================
# Round 4 -- who NAMED the key-file path, and the states that must not mint
# ==========================================================================


def test_the_four_tiers_each_report_who_named_the_path(tmp_path, isolated_config,
                                                       monkeypatch):
    """`cmd_serve` mints on an absent key file, and it may do that only when
    NOBODY named a path. The tier is returned rather than inferred, so this pins
    the tier and not only the path.

    The fourth row is the one an inference gets wrong: a config naming the
    DEFAULT path resolves to the same Path as no config at all, so a caller
    comparing against `default_keys_file()` reads a named path as a first start
    and mints on it."""
    cli = tmp_path / "from-cli.json"
    env_path = tmp_path / "from-env.json"
    global_cfg = tmp_path / "global-kb.toml"
    env = {keyfile.KEYS_FILE_ENV: str(env_path)}

    global_cfg.write_text(f'[serve]\nkeys_file = "{tmp_path / "from-config.json"}"\n')
    assert keyfile.resolve_keys_file_with_source(str(cli), env=env)[1] == \
        keyfile.SOURCE_CLI
    assert keyfile.resolve_keys_file_with_source(None, env=env)[1] == \
        keyfile.SOURCE_ENV
    assert keyfile.resolve_keys_file_with_source(None, env={})[1] == \
        keyfile.SOURCE_CONFIG

    monkeypatch.setenv("HOME", str(tmp_path))
    default = keyfile.default_keys_file()
    global_cfg.write_text(f'[serve]\nkeys_file = "{default}"\n')
    path, source = keyfile.resolve_keys_file_with_source(None, env={})
    assert path == default, "the fixture must name the default path"
    assert source == keyfile.SOURCE_CONFIG

    global_cfg.unlink()
    assert keyfile.resolve_keys_file_with_source(None, env={}) == (
        default, keyfile.SOURCE_DEFAULT)


def test_a_blank_keys_file_variable_refuses_instead_of_using_the_default(
        tmp_path, isolated_config, monkeypatch):
    """A set but blank $CONTEXTLAKE_KEYS_FILE is a shell that expanded nothing.

    Falling back to the default path answers it in the FAIL-OPEN direction: the
    default is usually absent, and `cmd_serve` mints an unscoped shared token on
    an absent default. $CONTEXTLAKE_MCP_TOKEN already answers the same input in
    the fail-closed direction, and the two variables must not read one shell
    accident in opposite ways.

    Unset is the other half of the split and stays the default path: without it
    an implementation that refuses on every empty read passes the first half."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(keyfile.KeyFileError) as caught:
            keyfile.resolve_keys_file_with_source(None,
                                                  env={keyfile.KEYS_FILE_ENV: blank})
        assert keyfile.KEYS_FILE_ENV in str(caught.value)
        # The refusal names the path it REFUSED to fall back to, so the operator
        # can see which location the blank value would have opened.
        assert str(keyfile.default_keys_file()) in str(caught.value)

    assert keyfile.resolve_keys_file_with_source(None, env={}) == (
        keyfile.default_keys_file(), keyfile.SOURCE_DEFAULT)
    # The CLI tier is read before the variable, so a blank one cannot take down a
    # run that named a path on the command line.
    assert keyfile.resolve_keys_file_with_source(
        str(tmp_path / "named.json"),
        env={keyfile.KEYS_FILE_ENV: "  "})[1] == keyfile.SOURCE_CLI


def test_resolve_keys_file_is_the_same_walk_without_the_source(tmp_path,
                                                               isolated_config,
                                                               monkeypatch):
    """The two-value function and the one-value one are one implementation.

    `keys_cmd` calls the short form and `cmd_serve` the long one. A second copy
    of the four-tier walk is how a caller's idea of the tier order drifts from
    the resolver's."""
    env_path = tmp_path / "from-env.json"
    env = {keyfile.KEYS_FILE_ENV: str(env_path)}
    monkeypatch.setenv("HOME", str(tmp_path))

    assert keyfile.resolve_keys_file(None, env=env) == env_path
    with pytest.raises(keyfile.KeyFileError):
        keyfile.resolve_keys_file(None, env={keyfile.KEYS_FILE_ENV: " "})


def test_an_unaccountable_record_state_never_reports_all_revoked(tmp_path, keys):
    """`all-revoked` is the one zero-live state `cmd_serve` mints on, and it was
    the last `return` of the chain: any record state this reader cannot name
    arrived there by arithmetic and turned a scoped file into a minted token.

    The fourth state is injected through the `keys_module` seam the file already
    uses. Nothing produces it today, which is the point: the default a credential
    path falls to is worth being right before an input reaches it."""
    class _Suspended(FakeRecord):
        def state(self, now):
            self._touched.append(self.id)
            return "suspended"

    class _Keys(FakeKeys):
        @property
        def KeyRecord(self):  # noqa: N802 - the real module's class name
            touched = self.touched

            class _Bound:
                @staticmethod
                def from_dict(data):
                    return _Suspended(data, touched)

            return _Bound

    home = make_dir(tmp_path / "d", 0o755)
    _key, rec = record("A")
    path = write_file(home / "mcp-keys.json", document([rec]), 0o600)

    ring = keyfile.Keyring.load(path, keys_module=_Keys())

    assert ring.live_count() == 0
    assert ring.key_status() == keyfile.STATUS_UNKNOWN
    assert ring.key_status() != keyfile.STATUS_ALL_REVOKED
    # The three real states still classify, so the guard is not "refuse
    # everything with no live key".
    _, revoked = record("B", revoked_at="2026-02-01T00:00:00Z")
    revoked_only = write_file(home / "revoked.json", document([revoked]), 0o600)
    assert keyfile.Keyring.load(revoked_only,
                                keys_module=keys).key_status() == \
        keyfile.STATUS_ALL_REVOKED


@POSIX_ONLY
def test_a_chmod_alone_is_invisible_until_the_content_changes(tmp_path, keys):
    """The residual `reload_if_changed` documents, measured in both directions.

    Every permission check on the reload path sits behind the freshness triple,
    so a mode or an owner change that leaves the CONTENT alone is not seen. That
    is true of the key file's own mode, not only of the parent directory, and
    the note used to name the parent alone.

    The bound is the second half: to USE the widened mode an attacker must write
    the file, a write moves the triple, and the moved triple runs the full report
    before anything is adopted. Both halves are here because the first alone
    reads as a hole and the second alone reads as coverage."""
    home = make_dir(tmp_path / "d", 0o755)
    key_a, rec_a = record("A")
    path = write_file(home / "mcp-keys.json", document([rec_a]), 0o600)
    ring = keyfile.Keyring.load(path, keys_module=keys)
    warned: list[str] = []
    ring._warn = warned.append
    before = stamp_of(path)

    os.chmod(path, 0o666)

    assert stamp_of(path) == before, "the chmod moved the triple; nothing is proven"
    assert ring.reload_if_changed() is False
    assert warned == []
    assert ring.resolve(key_a)[1] == "live", "the keys loaded before it still serve"

    # The other direction: the first CONTENT change is refused by the file mask,
    # and the keys loaded before it are kept.
    _, rec_b = record("B")
    atomic_replace(path, document([rec_a, rec_b]))
    os.chmod(path, 0o666)
    assert stamp_of(path) != before, "the write did not move the triple"

    assert ring.reload_if_changed() is False
    assert len(warned) == 1 and "0666" in warned[0]
    assert len(ring) == 1, "the widened file was adopted"
    assert ring.resolve(key_a)[1] == "live"


def test_no_source_claims_that_anything_schedules_kb_keys_prune():
    """Nothing schedules `kb keys prune`. It runs when a person types it.

    The claim was written into the reasoning for a fail-closed refusal: a key
    file holding no record was justified by "`kb keys prune` on a schedule
    empties a file whose keys lapsed, so the calendar reaches this state too".
    No timer, cron entry or in-process job calls `keys.prune`; the only caller
    is `kb/cmds/keys_cmd.py`, behind the `prune` verb. The BEHAVIOUR is right
    and unchanged -- an empty file admits nobody and reads like a first start --
    but the stated mechanism was false, and a false reason is what gets read by
    the next person deciding whether the refusal still applies.

    A test rather than a one-time edit because it was reported removed once and
    was still in the tree. Three files carried it: this module's own docstring
    for the no-records row, `kb/cmds/serve.py`'s table of zero-live states, and
    `tests/kb/test_serve_keyring.py`'s docstring for the same row. A phrase
    check, so it fails on the sentence coming back in any of them.
    """
    root = Path(keyfile.__file__).resolve().parents[1]
    files = [root / "kb" / "keyfile.py",
             root / "kb" / "cmds" / "serve.py",
             Path(__file__).resolve().parent / "test_serve_keyring.py"]
    banned = ("prune` on a schedule", "prune`` on a schedule",
              "no deliberate act", "without anyone deciding")

    for path in files:
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, f"{path} still claims: {phrase!r}"

    # The negative control, and it is narrow: it proves the files were found and
    # the refusal's state constant is still referenced, NOT that the refusal
    # fires. That is pinned by
    # tests/kb/test_serve_keyring.py::test_a_key_file_holding_no_records_at_all_
    # refuses_and_mints_nothing, which drives `cmd_serve` and reads the exit
    # code. This test guards the SENTENCE; that one guards the behaviour.
    assert "STATUS_NO_KEYS" in (root / "kb" / "cmds" / "serve.py").read_text()
    assert keyfile.STATUS_NO_KEYS == "no-keys"
