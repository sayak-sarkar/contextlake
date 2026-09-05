"""Guards for the key-file states `kb serve` must never answer with a token.

`kb/cmds/serve.py` mints and prints a shared bearer token on every network start
that finds no key file. That is correct for ONE state, ENOENT, and wrong for
every other. Round 1 modelled absent and present, so three states that are
neither reached the mint path:

* a DANGLING SYMLINK at the key-file path -- `Path.exists()` answers False;
* a key file this account cannot stat -- `Path.exists()` swallows
  `PermissionError` and answers False;
* (the same door) any other `OSError` from the stat.

Each one turned a scoped, per-key deployment into one unscoped token printed on
stderr, which is the downgrade the whole module exists to stop.

Round 2 closed those three and left a fourth open, reached with no operator
action at all. A VALID key file whose keys have ALL LAPSED held zero live keys,
which the serve path read as "treat it as absent", so it discarded every scoped
key, minted an unscoped shared token, printed it and exited 0. The reasoning
written down for that route -- "the operator revoked their last key and must not
be locked out with everyone else" -- describes a DECISION. An expiry is not one:
it is the default 90-day deadline arriving while nobody is looking.

So zero live keys is three states here, and each has a test below: every key
REVOKED (mints, and now says which state it is in), every key EXPIRED (refuses),
and no key records at all (refuses). The reverse direction is pinned too: a file
with live keys still serves scoped and mints nothing.

Nothing here is a real credential. No key file written by these tests holds a
drawn secret: the fixtures use `contextlake.kb.keys.key_from_bytes` over fixed,
public bytes, and every file lands under pytest's `tmp_path`. Symlinks are
created under `tmp_path` only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from contextlake.kb import keyfile


def _unreadable_dir_hides(path) -> bool:
    """Is the fixture's directory genuinely unreadable, on THIS Python?

    `Path.exists()` answers a locked-out path differently by version: it returns
    False on 3.14 and raises PermissionError on 3.10 to 3.13. An earlier version
    of these tests asserted the False, passed on the dev venv and failed all four
    knowledge-layer cells on CI.

    Either answer proves what the fixture needs to establish: the directory
    cannot be read. The thing under test is what `inspect_key_file` and
    `Keyring.load` DO about it, and that is portable because the production code
    catches OSError rather than reading a boolean.
    """
    try:
        return path.exists() is False
    except PermissionError:
        return True



POSIX_ONLY = pytest.mark.skipif(os.name != "posix",
                                reason="symlinks and mode bits are POSIX here")

# Public, fixed bytes. `mint()` draws a real random key and is never called.
_FIXTURE_BYTES = b"NOT-A-REAL-KEY-serve-fixture-000"[:32].ljust(32, b"0")


def _key_file(path: Path) -> Path:
    """One valid, live key file written by the real writer."""
    from contextlake.kb import keys as keys_mod

    key = keys_mod.key_from_bytes(_FIXTURE_BYTES)
    record = keys_mod.KeyRecord(id="k_serve1", name="fixture",
                                digest=keys_mod.digest(key),
                                created_at="2026-01-01T00:00:00Z")
    keyfile.write_document(path, [record])
    return path


def _record(tag: str, **fields):
    """One record over fixed, public bytes. `mint()` is never called here.

    The bytes differ per tag so two records in one file carry two digests: the
    writer refuses a file holding one digest twice.
    """
    from contextlake.kb import keys as keys_mod

    raw = (b"NOT-A-REAL-KEY-serve-" + tag.encode("ascii")).ljust(32, b"0")[:32]
    return keys_mod.KeyRecord(id=f"k_{tag}", name=tag,
                              digest=keys_mod.digest(keys_mod.key_from_bytes(raw)),
                              created_at="2026-01-01T00:00:00Z", **fields)


# Long past, and it parses. `KeyRecord.state` reads a stamp it cannot parse as
# expired too, so a fixture with a broken stamp would pass this test for the
# wrong reason.
_LAPSED = "2020-01-01T00:00:00Z"


def _serve_args(tmp_path: Path):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n\n[embeddings]\nenabled = false\n')
    return SimpleNamespace(config=str(cfg), transport="http", host=None, port=None)


def _run_serve(args, monkeypatch, *, keys_file, env_token=None, keys_only=False):
    """Drive `cmd_serve` with the socket and the minter stubbed.

    Returns `(rc, captured run_server kwargs, minted calls)`. `resolve_token` is
    counted rather than inferred from the banner: the exit code alone cannot see
    a credential that was drawn and then not printed.

    `env_token` pins `CONTEXTLAKE_MCP_TOKEN`, which is unset for every other
    row. Without it the tests here could not reach the branch where the operator
    has their own token, and a decision that only holds with the variable unset
    is half a decision.
    """
    from contextlake.kb import server as srv
    from contextlake.kb.cmds import serve as serve_mod

    captured: dict = {}
    minted: list[int] = []
    real_resolve = srv.resolve_token
    monkeypatch.setattr(srv, "run_server", lambda store, **kw: captured.update(kw))
    monkeypatch.setattr(srv, "resolve_token",
                        lambda: (minted.append(1), real_resolve())[1])
    monkeypatch.delenv(srv.TOKEN_ENV, raising=False)
    if env_token is not None:
        monkeypatch.setenv(srv.TOKEN_ENV, env_token)
    args.keys_file = None if keys_file is None else str(keys_file)
    args.keys_only = keys_only
    return serve_mod.cmd_serve(args), captured, minted


def _unnamed_default(tmp_path, monkeypatch):
    """Point the DEFAULT tier at an absent path, and cut the two tiers above it.

    `keys_file=None` alone does not reach the default: tier 3 reads
    `[serve] keys_file` from the developer's own `~/.contextlake/kb.toml`, and
    tier 2 reads the environment, so a test that skipped this would pass here and
    behave differently on another machine. Returns the default path so a caller
    can assert against it."""
    from contextlake.kb import config as kb_config
    from contextlake.kb import keyfile

    absent = tmp_path / "unnamed" / "mcp-keys.json"
    monkeypatch.setattr(kb_config, "GLOBAL_CONFIG", str(tmp_path / "no-global.toml"))
    monkeypatch.setattr(keyfile, "default_keys_file", lambda: absent)
    monkeypatch.delenv(keyfile.KEYS_FILE_ENV, raising=False)
    return absent


@POSIX_ONLY
def test_a_dangling_symlink_exits_1_and_mints_nothing(tmp_path, monkeypatch,
                                                      capsys):
    """The blocker. A broken link is a PRESENT path this reader cannot use, and
    it used to read as "no key file" -- so the server discarded every scoped key
    and printed one unscoped shared token instead."""
    link = tmp_path / "mcp-keys.json"
    link.symlink_to(tmp_path / "never-created.json")
    assert link.exists() is False, "the fixture does not reproduce the swallow"

    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=link)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {}, "the server started"
    assert minted == [], "a shared token was drawn"
    assert "Bearer token:" not in err
    assert "symlink" in err and str(link) in err


@POSIX_ONLY
@pytest.mark.skipif(os.geteuid() == 0, reason="root can stat anything")
def test_a_key_file_that_cannot_be_stat_ed_exits_1_and_mints_nothing(
        tmp_path, monkeypatch, capsys):
    """The second door: `Path.exists()` swallows PermissionError and answers
    False, so a key file the server cannot examine also read as absent."""
    home = tmp_path / "locked"
    home.mkdir()
    path = _key_file(home / "mcp-keys.json")
    os.chmod(home, 0o000)
    args = _serve_args(tmp_path)
    try:
        assert _unreadable_dir_hides(path), "the fixture does not lock the directory"
        rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)
    finally:
        os.chmod(home, 0o700)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {}
    assert minted == []
    assert "Bearer token:" not in err
    assert "cannot be examined" in err


@POSIX_ONLY
def test_a_symlinked_key_file_exits_1_however_tidy_its_modes_are(tmp_path,
                                                                 monkeypatch,
                                                                 capsys):
    """The link's own directory is 0700 and the target file is 0600, so both
    masks passed while the file itself sat in a 0777 directory that no check
    ever looked at."""
    tight = tmp_path / "tight"
    loose = tmp_path / "loose"
    tight.mkdir()
    loose.mkdir()
    os.chmod(tight, 0o700)
    # Written first, then widened: `write_document` refuses a 0777 parent and
    # tightens the one it writes into, so the fixture cannot be built the other
    # way round.
    target = _key_file(loose / "real.json")
    os.chmod(loose, 0o777)
    link = tight / "mcp-keys.json"
    link.symlink_to(target)
    assert os.stat(link).st_mode & keyfile.FILE_MASK == 0
    assert os.stat(tight).st_mode & keyfile.PARENT_MASK == 0
    assert os.stat(loose).st_mode & keyfile.PARENT_MASK != 0

    args = _serve_args(tmp_path)
    try:
        rc, captured, minted = _run_serve(args, monkeypatch, keys_file=link)
    finally:
        os.chmod(loose, 0o700)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {}
    assert minted == []
    assert "symlink" in err


def test_a_valid_key_file_still_starts_and_suppresses_the_token(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """The positive control for all three refusals above. Without it a serve
    path that refused every key file would score green on the lot."""
    path = _key_file(tmp_path / "mcp-keys.json")
    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    err = capsys.readouterr().err
    assert rc == 0
    assert minted == []
    assert captured["token"] is None
    assert captured["keyring"] is not None
    assert "1 keys loaded from" in err


def test_the_skipped_permission_note_reaches_the_operator(tmp_path, monkeypatch,
                                                          capsys):
    """`PERMISSION_CHECK_SKIPPED` had no reader for a round.

    Off POSIX the two masks and the owner check do not run, so a start there
    must say so out loud: a check that quietly passes reads as protection it
    does not provide. Both directions asserted -- on POSIX the note must NOT
    appear, or "printed always" would pass the first half.
    """
    path = _key_file(tmp_path / "mcp-keys.json")
    args = _serve_args(tmp_path)

    rc, _captured, _minted = _run_serve(args, monkeypatch, keys_file=path)
    assert rc == 0
    assert "SKIPPED" not in capsys.readouterr().err

    monkeypatch.setattr(keyfile, "POSIX", False)
    rc, captured, _minted = _run_serve(args, monkeypatch, keys_file=path)
    err = capsys.readouterr().err
    assert rc == 0
    assert keyfile.PERMISSION_CHECK_SKIPPED in err
    assert captured["keyring"] is not None


def test_serve_makes_no_filesystem_check_of_its_own(tmp_path, monkeypatch,
                                                    capsys):
    """The key-file path is read ONCE, by `Keyring.load`, and `cmd_serve` adds
    no check of its own.

    Counted at the SYSCALL and not at `keyfile.counters()`: the check this
    replaced was `Path.exists()`, which goes straight to `os.stat` and never
    reaches the module's own accessor, so a counter-based assertion would read
    0 while the second check was still there.
    """
    path = _key_file(tmp_path / "mcp-keys.json")
    seen: list[str] = []
    real_stat, real_lstat = os.stat, os.lstat

    def counting(real):
        def wrapper(target, *args, **kwargs):
            if str(target) == str(path):
                seen.append(real.__name__)
            return real(target, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(os, "stat", counting(real_stat))
    monkeypatch.setattr(os, "lstat", counting(real_lstat))
    args = _serve_args(tmp_path)
    rc, captured, _minted = _run_serve(args, monkeypatch, keys_file=path)

    assert rc == 0 and captured["keyring"] is not None
    assert seen == ["lstat"], seen


def test_a_key_file_whose_keys_all_expired_refuses_and_mints_nothing(
        tmp_path, monkeypatch, capsys):
    """THE BLOCKER. No operator action reaches this state: the default expiry
    is 90 days, and the deadline arrives on its own.

    Before the split, such a file held zero live keys, the serve path read that
    as "treat it as absent", and the server discarded every scoped key, minted
    one unscoped shared token, printed it and exited 0. A date passing must not
    turn a scoped server into an open one.
    """
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [_record("exp1", expires_at=_LAPSED)])

    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {}, "the server started"
    assert minted == [], "a shared token was drawn because a date passed"
    assert "Bearer token:" not in err
    # Constraint 3: the operator is told, in terms naming the state.
    assert "EXPIRED" in err and str(path) in err
    assert "contextlake kb keys create" in err


def test_a_key_file_whose_keys_were_all_revoked_mints_and_names_the_state(
        tmp_path, monkeypatch, capsys):
    """The other half of the split, and the one state that still mints.

    Revoking is a deliberate act by a person at the terminal, so this start
    hands them a shared token rather than locking them out of their own server.
    It has to SAY so: the same three lines used to print for a key file that did
    not exist, and a banner that cannot tell those apart is how the expired case
    above went unnoticed.
    """
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path,
                           [_record("rev1", revoked_at="2026-02-02T00:00:00Z")])

    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    err = capsys.readouterr().err
    assert rc == 0
    assert minted == [1], "the operator was locked out of their own server"
    assert captured["token"]
    assert captured["keyring"] is None
    assert "Bearer token:" in err
    # The state, named. Without this the operator reads a minted-token banner
    # and cannot tell it from a first start with no key file at all.
    assert "REVOKED" in err and str(path) in err
    assert "UNSCOPED" in err


def test_a_key_file_holding_no_records_at_all_refuses_and_mints_nothing(
        tmp_path, monkeypatch, capsys):
    """The third zero-live state. Nothing schedules `kb keys prune`, so no timer
    empties a key file; a person running it by hand does. The refusal does not
    rest on who emptied it. A file holding no record admits nobody and reads on
    stderr like a first start, so minting on it would turn a deployment that
    asked for scoped keys into an open one."""
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [])

    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {}
    assert minted == []
    assert "Bearer token:" not in err
    assert "no key records" in err and str(path) in err


def test_a_revoked_key_beside_an_expired_one_refuses(tmp_path, monkeypatch,
                                                     capsys):
    """A deliberate act in the file does not launder the date that passed.

    One record answers "why did this key stop working", and revoked beats
    expired there. The FILE answers "may this server downgrade itself", and one
    lapsed key is enough to say no.
    """
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [
        _record("mix-r", revoked_at="2026-02-02T00:00:00Z"),
        _record("mix-e", expires_at=_LAPSED),
    ])

    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {}
    assert minted == []
    assert "EXPIRED" in err


def test_one_live_key_beside_an_expired_one_still_serves_scoped(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """The reverse direction. A refusal that fired whenever ANY key had lapsed
    would take down every deployment that ever rotated a key, and it would score
    green on every row above."""
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [
        _record("still-live"),
        _record("long-gone", expires_at=_LAPSED),
    ])

    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    err = capsys.readouterr().err
    assert rc == 0
    assert minted == [], "a shared token was drawn beside a live key"
    assert captured["token"] is None
    assert captured["keyring"] is not None
    assert "1 keys loaded from" in err
    assert "Bearer token:" not in err


def test_expired_keys_and_a_pinned_token_start_without_minting(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """The branch the other rows cannot see: every key expired AND
    `CONTEXTLAKE_MCP_TOKEN` pinned.

    The token was already live beside the scoped keys and its holder could read
    the whole graph yesterday, so the expiry granted nobody anything new.
    Refusing here would take a working deployment down over a date, and the
    defect being stopped is drawing a NEW unscoped credential unattended. So
    this starts, mints nothing, and names the state.
    """
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [_record("exp2", expires_at=_LAPSED)])

    args = _serve_args(tmp_path)
    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path,
                                      env_token="fake-pinned-value-for-a-test")

    err = capsys.readouterr().err
    assert rc == 0
    assert minted == [], "a shared token was minted beside the pinned one"
    assert captured["token"] == "fake-pinned-value-for-a-test"
    # Kept, so an expired key reads as expired and `kb keys create` restores
    # scoped auth on the next reload rather than at the next restart.
    assert captured["keyring"] is not None
    assert "EXPIRED" in err
    assert "No shared token was minted" in err
    assert "read from $CONTEXTLAKE_MCP_TOKEN" in err
    # The value itself is never echoed back: the operator already has it.
    assert "fake-pinned-value-for-a-test" not in err


# ==========================================================================
# Round 4 -- an absent file at a path somebody NAMED
# ==========================================================================


@pytest.mark.parametrize("tier", ["cli", "env", "config"])
def test_a_named_key_file_that_is_not_there_refuses_and_mints_nothing(
        tmp_path, monkeypatch, capsys, tier):
    """Naming a path says the keys live there. Nothing there is a fault.

    The case that decided it is a container whose volume mount did not appear:
    the named path is empty, and before this the server minted one UNSCOPED
    shared token, printed it and exited 0, with stderr indistinguishable from a
    first start on a fresh machine. Nothing told the operator the deployment was
    open.

    All three naming tiers, because a hole in one of them is the whole defect
    back: `--keys-file` is the container's own flag, and the two below it are the
    ways a deployment sets the same thing without a flag."""
    from contextlake.kb import config as kb_config
    from contextlake.kb import keyfile

    absent = tmp_path / "mount" / "mcp-keys.json"
    _unnamed_default(tmp_path, monkeypatch)
    args = _serve_args(tmp_path)
    keys_file = None
    if tier == "cli":
        keys_file = absent
    elif tier == "env":
        monkeypatch.setenv(keyfile.KEYS_FILE_ENV, str(absent))
    else:
        global_cfg = tmp_path / "global-kb.toml"
        global_cfg.write_text(f'[serve]\nkeys_file = "{absent}"\n')
        monkeypatch.setattr(kb_config, "GLOBAL_CONFIG", str(global_cfg))

    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=keys_file)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {}, "the server started"
    assert minted == [], "a token was drawn for a server that never started"
    assert "Bearer token:" not in err
    # The path AND the tier are named: with four places a path can come from,
    # an operator debugging this has to see which one was consulted.
    assert str(absent) in err
    assert {"cli": "--keys-file", "env": f"${keyfile.KEYS_FILE_ENV}",
            "config": "[serve] keys_file"}[tier] in err


def test_no_key_file_and_nobody_named_one_still_mints(tmp_path, monkeypatch,
                                                      capsys):
    """The reverse direction, and the reason the split exists.

    A first start on a machine that has never issued a key must still come up.
    Without this row, "refuse when the file is missing" passes the test above and
    breaks every fresh install."""
    absent = _unnamed_default(tmp_path, monkeypatch)
    args = _serve_args(tmp_path)

    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=None)

    err = capsys.readouterr().err
    assert rc == 0
    assert minted == [1], "the first start drew no token"
    assert captured["token"] and captured["keyring"] is None
    assert "Bearer token:" in err
    assert not absent.exists(), "the fixture created the file it says is absent"


def test_a_blank_keys_file_variable_refuses_the_start(tmp_path, monkeypatch,
                                                      capsys):
    """A set but blank $CONTEXTLAKE_KEYS_FILE reaches `cmd_serve` as a refusal,
    not as the default path.

    The refusal is raised by the resolver, so this pins that `_load_keyring`
    catches it: uncaught it would reach `cli.py`'s top-level guard and print a
    traceback where the operator needs two lines and exit 1."""
    from contextlake.kb import keyfile

    _unnamed_default(tmp_path, monkeypatch)
    monkeypatch.setenv(keyfile.KEYS_FILE_ENV, "   ")
    args = _serve_args(tmp_path)

    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=None)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {} and minted == []
    assert "Bearer token:" not in err
    assert keyfile.KEYS_FILE_ENV in err


def test_a_key_file_this_reader_cannot_account_for_refuses(tmp_path, monkeypatch,
                                                           capsys):
    """The fifth content state, injected through the `keys_module` seam.

    `all-revoked` is the one zero-live state that mints and it was the last
    `return` of `key_status`'s chain, so a record state this reader cannot name
    minted an unscoped token. Routed here explicitly rather than left to fall
    through: the branch after the minting one starts the server with a keyring
    holding no live key and no token, which is a total lockout at exit 0."""
    from contextlake.kb import keyfile

    real_load = keyfile.Keyring.load

    def _load_with_unknown_state(path, **kw):
        ring = real_load(path, **kw)
        monkeypatch.setattr(ring, "key_status",
                            lambda: keyfile.STATUS_UNKNOWN, raising=False)
        return ring

    path = _key_file(tmp_path / "mcp-keys.json")
    monkeypatch.setattr(keyfile.Keyring, "load", staticmethod(_load_with_unknown_state))
    args = _serve_args(tmp_path)

    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    err = capsys.readouterr().err
    assert rc == 1
    assert captured == {} and minted == []
    assert "Bearer token:" not in err
    assert str(path) in err


# --- the order the operator reads it in ------------------------------------


class _OrderedOutput:
    """One buffer for `sys.stderr` AND `serve.log()`, so ORDER is testable.

    `cmd_serve` writes the banner through `log()`, which lands on stdout on a
    network start, and every credential and refusal line through
    `print(file=sys.stderr)`. `capsys` keeps those two apart, so a test reading
    `.out` and `.err` can see both strings and say nothing about which came
    first -- and which came first WAS the defect: the green banner printed, then
    the refusal, and an operator reading top to bottom saw a running server.

    `print()` writes the text and the newline as two separate `write` calls, so
    the entries are joined into one blob rather than read as lines.
    """

    def __init__(self):
        self.parts: list[str] = []

    def write(self, text):  # sys.stderr
        self.parts.append(text)
        return len(text)

    def flush(self):  # sys.stderr
        return None

    def log(self, message, *args, **kwargs):  # contextlake.logging_setup.log
        self.parts.append(f"{message}\n")

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _capture_in_order(monkeypatch) -> _OrderedOutput:
    """Point both of `cmd_serve`'s output channels at one buffer."""
    from contextlake.kb.cmds import serve as serve_mod

    sink = _OrderedOutput()
    monkeypatch.setattr(sys, "stderr", sink)
    monkeypatch.setattr(serve_mod, "log", sink.log)
    return sink


# Every refusal in `cmd_serve` that can be reached without a permission trick,
# as (name, setup) where setup returns the `keys_file` and `keys_only` to use
# and the string the refusal must carry. Parametrised rather than written once
# against the reproduced case: the banner sat above ALL of these, so a fix
# checked on one of them proves nothing about the other four.
def _refusal_no_key_file_and_keys_only(tmp_path, monkeypatch):
    _unnamed_default(tmp_path, monkeypatch)
    return {"keys_file": None, "keys_only": True}, "--keys-only refused"


def _refusal_named_path_is_absent(tmp_path, monkeypatch):
    return {"keys_file": tmp_path / "mounted" / "mcp-keys.json"}, "--keys-file"


def _refusal_every_key_expired(tmp_path, monkeypatch):
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [_record("bexp", expires_at=_LAPSED)])
    return {"keys_file": path}, "EXPIRED"


def _refusal_no_records_at_all(tmp_path, monkeypatch):
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [])
    return {"keys_file": path}, "no key records at all"


def _refusal_keys_only_with_a_pinned_token(tmp_path, monkeypatch):
    _unnamed_default(tmp_path, monkeypatch)
    return ({"keys_file": None, "keys_only": True,
             "env_token": "NOT-A-REAL-TOKEN-fixture"},
            "$CONTEXTLAKE_MCP_TOKEN is set")


@pytest.mark.parametrize("build", [
    _refusal_no_key_file_and_keys_only,
    _refusal_named_path_is_absent,
    _refusal_every_key_expired,
    _refusal_no_records_at_all,
    _refusal_keys_only_with_a_pinned_token,
], ids=["keys-only-no-file", "named-path-absent", "all-expired", "no-records",
        "keys-only-with-pinned-token"])
def test_a_refused_start_never_says_the_server_is_up(tmp_path, monkeypatch,
                                                     build):
    """THE BLOCKER, and it is stronger than an ordering claim.

    Reproduced live before the fix:

        $ HOME=<fresh> contextlake kb serve --transport http --keys-only
        ✓ MCP server on http://127.0.0.1:8765/mcp  (Ctrl-C to stop)
          --keys-only refused: no key file with a live key was found ...

    exit 1, and nothing listening. The banner printed above the credential
    block, so every refusal in that block printed second. On a start that
    refuses, the right assertion is not "the banner came later" but "there is no
    banner at all", which is what this checks. Both of `cmd_serve`'s banner
    lines are named: "Serving knowledge graph over MCP" is as much a claim that
    the server is up as the green line under it.
    """
    kwargs, must_say = build(tmp_path, monkeypatch)
    args = _serve_args(tmp_path)
    sink = _capture_in_order(monkeypatch)

    rc, captured, minted = _run_serve(args, monkeypatch, **kwargs)

    out = sink.text
    assert rc == 1
    assert captured == {}, "the server started"
    assert minted == [], "a credential was drawn for a start that refused"
    assert must_say in out, out
    assert "MCP server on" not in out, out
    assert "Serving knowledge graph over MCP" not in out, out


def test_the_banner_comes_after_every_line_the_start_refuses_on(tmp_path,
                                                                monkeypatch):
    """The other direction, on the one zero-live state that still starts.

    A start that goes ahead prints BOTH the credential lines and the banner, so
    "the banner is absent" cannot be the test here. This is the row where order
    is the whole claim, and it needs the shared buffer: with `capsys` the two
    strings sit in different streams and every ordering passes.

    The all-revoked path is used because it says the most before it starts --
    the state, the word UNSCOPED, and the way back to scoped auth.
    """
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path,
                           [_record("ordrev", revoked_at="2026-02-02T00:00:00Z")])
    args = _serve_args(tmp_path)
    sink = _capture_in_order(monkeypatch)

    rc, captured, minted = _run_serve(args, monkeypatch, keys_file=path)

    out = sink.text
    assert rc == 0 and minted == [1] and captured["token"]
    for said in ("REVOKED", "UNSCOPED", "Bearer token:"):
        assert said in out, out
    assert "MCP server on" in out, out
    # The claim itself. Reverted, the banner index is 0-ish and every one of
    # these flips.
    banner = out.index("MCP server on")
    for said in ("REVOKED", "UNSCOPED", "Bearer token:", "kb keys create"):
        assert out.index(said) < banner, f"{said!r} printed after the banner\n{out}"


def test_the_first_start_names_the_command_that_issues_a_scoped_key(
        tmp_path, monkeypatch, capsys):
    """A new operator has to learn that `kb keys` exists on the path they walk.

    Every refusal named `contextlake kb keys create`. The happy path -- a first
    start on a machine with no key file, which is the one route a new operator
    takes -- named nothing, printed a shared token and left them using it. The
    person who most needs a scoped key was the only one never told the command.

    Pinned against the revoked path below it, which already named the command
    for its own reason: without that half, moving the line into the shared
    branch would pass while saying it twice on one start.
    """
    _unnamed_default(tmp_path, monkeypatch)
    args = _serve_args(tmp_path)

    rc, _captured, minted = _run_serve(args, monkeypatch, keys_file=None)

    err = capsys.readouterr().err
    assert rc == 0 and minted == [1]
    assert "contextlake kb keys create" in err, err
    assert "UNSCOPED" in err, err

    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path,
                           [_record("m2rev", revoked_at="2026-02-02T00:00:00Z")])
    rc, _captured, _minted = _run_serve(args, monkeypatch, keys_file=path)
    err = capsys.readouterr().err
    assert rc == 0
    assert err.count("contextlake kb keys create") == 1, err
    assert "UNSCOPED and shared" not in err, err

    # The third way into the same branch, and the one nothing covered: every key
    # revoked AND $CONTEXTLAKE_MCP_TOKEN pinned. `first_start` is read before
    # `keyring` is cleared, so it is False here; a guard that read the cleared
    # value would say "first start" on a file the operator has been running for
    # months and add the line to a banner that already names the command.
    rc, _captured, _minted = _run_serve(args, monkeypatch, keys_file=path,
                                        env_token="NOT-A-REAL-TOKEN-fixture")
    err = capsys.readouterr().err
    assert rc == 0
    assert "REVOKED" in err and "read from $CONTEXTLAKE_MCP_TOKEN" in err
    assert err.count("contextlake kb keys create") == 1, err
    assert "UNSCOPED and shared" not in err, err


def test_the_serve_table_is_not_reported_as_unknown(tmp_path, monkeypatch):
    """One run said `[serve]` was unknown and refused the start over `[serve]
    keys_file`. Two lines, opposite claims.

    Reproduced live before the fix:

        config: unknown config table 'serve' (ignored)          <- stdout
        Key file refused: [serve] keys_file names ... exit 1    <- stderr

    The table IS known: `keyfile._serve_keys_file` opens the same TOML and
    honours the value, which is how the second line came to name it. So the
    warning was the wrong line, and `_TABLES` gains "serve".

    Both halves in one test, because either alone reads as fine. The negative
    control keeps the fix narrow: a real typo must still be warned about, or
    "stop warning" would pass the first half.
    """
    from contextlake.kb import config as kb_config

    cfg = tmp_path / "kb.toml"
    keys = tmp_path / "mcp-keys.json"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n\n'
                   f'[serve]\nkeys_file = "{keys}"\n\n[srve]\nkeys_file = "x"\n')

    said: list[str] = []
    monkeypatch.setattr(kb_config, "log",
                        lambda message, *a, **kw: said.append(str(message)))
    kb_config.load_kb_config(str(cfg))

    warnings = "\n".join(said)
    assert "unknown config table 'serve'" not in warnings, warnings
    assert "unknown config table 'srve'" in warnings, warnings

    # The other half of the contradiction, from the reader that honours it.
    monkeypatch.delenv(keyfile.KEYS_FILE_ENV, raising=False)
    path, source = keyfile.resolve_keys_file_with_source(None, config_path=str(cfg))
    assert path == keys and source == keyfile.SOURCE_CONFIG
