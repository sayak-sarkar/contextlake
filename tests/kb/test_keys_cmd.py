"""`contextlake kb keys` -- registration, exit codes, and the display-once rule.

Every test drives the real CLI through `cli.main`, because three of the things
under test only exist there: the `_KB_COMMANDS` route, the console handler that
`setup_logging` binds to `sys.stdout`, and the `--log-file` rotating handler.
Calling `cmd_keys(args)` directly would skip all three and pass while the shipped
command was broken.

**Read this before touching the stdout assertions.** `logging_setup.py:222` builds
`_ConsoleHandler(sys.stdout)` and binds the stream AT SETUP TIME. If the handler
were bound before pytest swapped `sys.stdout`, the capture would see nothing and
`assert 0 key matches on stdout` would pass no matter what the code did -- and so
would the break-test that routes the key through `log()`, which is the one thing
that break-test exists to catch. `setup_logging` calls `logger.handlers.clear()`
first and `cli.main` calls it on every run, so the binding is correct here. That
is an argument, not a measurement, so `_assert_capture_is_live` measures it: every
test that asserts a key is ABSENT from a stream first asserts a known non-secret
line is PRESENT on it. A vacuous capture fails the control, not the assertion.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from types import SimpleNamespace

import pytest

from contextlake import cli
from contextlake.kb import keyfile
from contextlake.kb import keys as keys_mod
from contextlake.kb.cmds import keys_cmd

# Criterion 4's regex, verbatim. `ctxlake_` plus the 43-character body plus the
# six-character checksum is 49 base62 characters after the prefix.
KEY_RE = re.compile(r"ctxlake_[0-9A-Za-z]{49}")


def _keys_parser():
    """The keys subparser as `cli.build_parser` actually builds it.

    Every verb-enumerating test reads `choices=` from HERE, never from a literal
    list and never from `keys_cmd.ACTIONS`. Those are two separate sources that
    can drift, and a hard-coded count keeps passing over the verbs it names while
    a newly added eighth verb goes uncovered with nothing red. S4.5.4 adds
    `usage` to this list in phase 3.
    """
    return cli.build_parser()._all_parsers["keys"]


def _parser_choices(dest: str) -> tuple:
    """The `choices=` on one argument, or `()` when it has none.

    `()` rather than letting `tuple(None)` raise. A `TypeError` from this helper
    would make a removed `choices=` fail as a broken test rather than as the
    assertion that the choices are missing, and a break-test has to fail for the
    reason it names.
    """
    action = {a.dest: a for a in _keys_parser()._actions}[dest]
    return tuple(action.choices or ())


def _verbs() -> tuple:
    return _parser_choices("action")


def _args_for(verb: str) -> list[str]:
    """The shortest argv that gets `verb` past argparse and into the handler.

    A verb refused at exit 2 for a missing positional never reaches the code the
    caller wants to test, so the "every verb" tests would pass on the parser
    alone.
    """
    if verb in ("create", "rotate", "show", "revoke"):
        return [verb, "target-name"]
    if verb == "prune":
        return [verb, "--before", "2020-01-01"]
    return [verb]


class Result:
    def __init__(self, code, out, err):
        self.code = code
        self.out = out
        self.err = err


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    """The key file every test writes, named through $CONTEXTLAKE_KEYS_FILE.

    Under `tmp_path` only. The live store at ~/Work/ContextLake/workspace/kb is
    never read and never written, and `resolve_keys_file` reads the environment
    tier ahead of the config tier, so nothing here can reach a real key file.
    """
    path = tmp_path / "keys" / "mcp-keys.json"
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o700)
    monkeypatch.setenv(keyfile.KEYS_FILE_ENV, str(path))
    return path


@pytest.fixture
def run(capsys, monkeypatch):
    """Run one `contextlake kb keys ...` and return its exit code and streams.

    `main` always leaves through `sys.exit`, so the SystemExit is the result, not
    an error. stdin is replaced on every run: `_cmd_check` reads it
    unconditionally, and a run that inherits the real stdin blocks the whole
    suite on a terminal.
    """

    def _run(*argv, stdin=""):
        monkeypatch.setattr(sys, "stdin", _Stdin(stdin))
        try:
            cli.main(["kb", "keys", *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        else:  # pragma: no cover - main always exits
            code = 0
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return _run


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text

    def isatty(self):
        return False


def _assert_capture_is_live(stream: str, needle: str, where: str) -> None:
    """Prove the capture sees this stream before believing a key is absent from it.

    `caplog` is vacuous here (`setup_logging` sets `propagate = False`) and a
    console handler bound to a pre-swap stdout would make `capsys` vacuous the
    same way. An absent-key assertion over an empty capture is not evidence, so
    every one of them is paired with this positive control.
    """
    assert needle in stream, (
        f"positive control failed: {where} did not carry {needle!r}, so this "
        "capture is not seeing the stream and any 'the key is absent' assertion "
        "over it would pass vacuously"
    )


# ---------------------------------------------------------------------------
# Criterion 2 -- registered in all three places
# ---------------------------------------------------------------------------


def test_kb_keys_is_registered_in_all_three_places():
    """A verb can parse, dispatch and still never route. All three or none.

    `_KB_COMMANDS` is the one that fails silently: `cli.py`'s kb branch routes on
    that frozenset, so without the entry the parser accepts `kb keys list`, the
    dispatch table holds a handler nothing calls, and the command falls through
    to the mirror tier. Nothing raises.
    """
    from contextlake.kb import cmds as kb_cmds

    assert "keys" in cli._KB_COMMANDS
    assert "keys" in kb_cmds.VERBS
    assert "keys" in kb_cmds._EAGER_HANDLERS
    assert kb_cmds._EAGER_HANDLERS["keys"] is keys_cmd.cmd_keys


def test_kb_keys_list_routes_end_to_end(run, keys_file):
    """The route itself, not the registration constants.

    `test_kb_keys_is_registered_in_all_three_places` reads three containers. This
    one proves a real invocation reaches the handler, which is what breaks when
    `_KB_COMMANDS` loses the entry while the other two keep it.
    """
    result = run("list")
    assert result.code == 0
    assert "live key(s)" in result.out


def test_the_parser_the_handler_and_the_dispatch_table_name_the_same_verbs():
    """Three lists of verbs in two files. Pin them, or they drift one at a time.

    `cli.py` spells its `choices=` out rather than importing `keys_cmd`, so the
    keystore stays off `contextlake mirror`'s startup path. That is the right
    call and it is exactly what lets the two lists diverge, so this is the test
    that makes the duplication safe. `kb source --type` carries the same pattern
    for the same reason (`cli.py:1137-1140`).
    """
    assert set(_verbs()) == set(keys_cmd.ACTIONS)
    assert set(_verbs()) == set(keys_cmd._DISPATCH)
    assert set(keys_cmd.WRITE_ACTIONS) | set(keys_cmd.READ_ACTIONS) == set(_verbs())
    assert not set(keys_cmd.WRITE_ACTIONS) & set(keys_cmd.READ_ACTIONS)


def test_the_client_choices_name_every_supported_and_every_refused_client():
    """`--client` must name the refused values too, or argparse answers for them.

    Leaving `claude-desktop` out of `choices=` gives the operator argparse's bare
    "invalid choice", which says neither why it is refused nor what to do
    instead. Naming it lets it reach the handler, which prints the mcp-remote
    route.
    """
    assert set(_parser_choices("client")) == set(keys_cmd.CLIENTS) | set(
        keys_cmd.REFUSED_CLIENTS
    )


def test_an_unrecognised_client_is_refused_rather_than_rendered_as_zed(run, keys_file):
    """The defect `choices=` closes, stated as behaviour rather than as a schema.

    `_client_block` ends in an unguarded `return` that renders the Zed block, so
    without `choices=` on `--client` every unrecognised value fell through to it:
    `--client nonsense-editor` created the key and printed Zed's settings.json at
    exit 0. Asserting the choices tuple alone does not catch that, and neither
    does `--client claude-desktop`, which the handler refuses either way.
    """
    result = run("create", "alice", "--client", "nonsense-editor")
    assert result.code == 2
    assert "context_servers" not in result.out


def test_the_policy_flags_the_handler_reads_all_exist_on_the_parser():
    """`_policy` reads six flags by name. A missing one is silently always-unset.

    This is the defect the round found in the tree: `_policy` read `--tools`,
    `--owners`, `--rate`, `--burst` and `--cost-budget`, the parser defined none
    of them, and every key was created with an empty policy block while
    `getattr(args, "tools", None)` returned None and nothing raised.
    """
    flags = set(_keys_parser()._option_string_actions)
    for flag in ("--tools", "--repos", "--owners", "--rate", "--burst",
                 "--cost-budget", "--external"):
        assert flag in flags, f"{flag} is read by _policy but not defined on the parser"


# ---------------------------------------------------------------------------
# Criterion 1 and 3 -- exit codes
# ---------------------------------------------------------------------------


def test_kb_keys_with_no_action_exits_2(capsys):
    """A required positional, so argparse refuses before any handler runs."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["kb", "keys"])
    assert excinfo.value.code == 2


def test_every_exit_code_row(run, keys_file, tmp_path):
    """One case per row of the ticket's exit-code table.

    The rows are asserted together because their VALUE is the contrast between
    them: 0 and 1 and 2 each have to be reachable for the others to mean
    anything, and a table split across nine test functions stops reading as a
    table.
    """
    # 0 -- list on a key file that does not exist yet.
    assert run("list").code == 0

    # 0 -- create, show, revoke, rotate, prune on a valid target.
    assert run("create", "alice").code == 0
    key_id = _only_id(keys_file)
    assert run("show", key_id).code == 0
    assert run("rotate", key_id).code == 0
    assert run("revoke", key_id).code == 0
    assert run("prune", "--before", "2020-01-01").code == 0

    # 1 -- show, revoke and rotate on an unknown id.
    for verb in ("show", "revoke", "rotate"):
        assert run(verb, "k_nosuch").code == 1, verb

    # 1 -- check on a malformed key.
    assert run("check", stdin="ctxlake_bogus").code == 1

    # 2 -- a missing required positional.
    assert run("create").code == 2
    assert run("show").code == 2

    # 2 -- prune with no --before. It deletes permanently, so it is never
    # defaulted.
    assert run("prune").code == 2

    # 2 -- a bad flag value. --expires is validated by the keystore, --client by
    # argparse's own choices=.
    assert run("create", "bob", "--expires", "yesterday").code == 2
    assert run("create", "bob", "--client", "claude-desktop").code == 2
    assert run("prune", "--before", "yesterday").code == 2


def _only_id(keys_file) -> str:
    document = json.loads(keys_file.read_text())
    live = [k for k in document["keys"] if not k.get("revoked_at")]
    return live[-1]["id"]


def test_revoke_on_an_unknown_id_exits_1(run, keys_file):
    """Deliberately NOT `kb source remove`'s documented exit-0 no-op.

    An admin scripting a revocation reads the exit code. "I revoked nothing" must
    never read as success, so this asymmetry is the point rather than an
    oversight, and `cli.py`'s epilog says so where a reader meets it.
    """
    run("create", "alice")
    result = run("revoke", "k_deadbe")
    assert result.code == 1
    assert "k_deadbe" in result.out


def test_keys_check_refuses_a_key_on_the_command_line(run, keys_file):
    """A key in argv lands in shell history and shows in `ps` to every account."""
    result = run("create", "alice")
    key = KEY_RE.search(result.err).group(0)
    result = run("check", key)
    assert result.code == 2
    assert "STDIN" in result.out or "stdin" in result.out
    # And the same key through the channel it is meant to arrive on works, so
    # the refusal above is about the CHANNEL and not about the key being bad.
    assert run("check", stdin=key).code == 0


def test_print_key_refuses_a_tty(run, keys_file, monkeypatch):
    """Both branches, with `isatty` patched. A key in scrollback is not a pipe."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    result = run("create", "alice", "--print-key")
    assert result.code == 2
    assert KEY_RE.search(result.out) is None
    assert "--print-key" in result.out

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    result = run("create", "bob", "--print-key")
    assert result.code == 0
    # Criterion 7: the bare key and nothing else on stdout.
    assert result.out.strip() == KEY_RE.search(result.out).group(0)


def test_claude_desktop_and_claude_web_are_refused_with_a_route(run, keys_file):
    """Refused, and each names where to go instead. Exit 2, not a rendered block."""
    result = run("create", "alice", "--client", "claude-web")
    assert result.code == 2
    assert "OAuth" in result.out

    result = run("create", "alice", "--client", "claude-desktop")
    assert result.code == 2
    assert "mcp-remote" in result.out
    assert "UNVERIFIED" in result.out


# ---------------------------------------------------------------------------
# Criteria 4 to 7 and 17 -- display once
# ---------------------------------------------------------------------------


def test_the_created_key_never_reaches_stdout_or_the_log_file(
    run, keys_file, tmp_path, capsys, monkeypatch
):
    """The whole display-once rule, on the three streams a key could reach.

    Two assertions have to move when the key is routed through `log()` instead of
    `print(..., file=sys.stderr)`: the stdout count, because
    `logging_setup.py:222` adds a `_ConsoleHandler(sys.stdout)` unconditionally,
    and the log-file count, because `logging_setup.py:229` adds a
    `RotatingFileHandler` whenever `--log-file` is set. `observability.redact`
    rewrites workspace paths and repo names, so it would scrub neither.
    """
    log_path = tmp_path / "run.log"
    monkeypatch.setattr(sys, "stdin", _Stdin(""))
    try:
        cli.main(["kb", "--log-file", str(log_path), "keys", "create", "alice"])
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()
    log_text = log_path.read_text()

    # The controls come first. Each names a line that is NOT a secret and that
    # the same code path emits onto the same stream, so a stream this capture
    # cannot see fails here rather than passing the assertion below it.
    _assert_capture_is_live(captured.out, "Created key", "stdout")
    _assert_capture_is_live(log_text, "Created key", "the --log-file file")

    # Counted into one dict and asserted once, rather than three asserts in a
    # row. Python stops at the first failing assert, so three asserts would show
    # only the stderr count moving when the key is routed through `log()`, while
    # the ticket requires BOTH the stdout count and the log-file count to move
    # and be seen to move. One dict puts all three numbers in the failure.
    counts = {"stderr": len(KEY_RE.findall(captured.err)),
              "stdout": len(KEY_RE.findall(captured.out)),
              "log file": len(KEY_RE.findall(log_text))}
    assert counts == {"stderr": 1, "stdout": 0, "log file": 0}


def test_list_and_show_never_print_a_key(run, keys_file):
    """Criterion 5. The digest is stored; there is nothing to print."""
    created = run("create", "alice")
    assert len(KEY_RE.findall(created.err)) == 1
    key_id = _only_id(keys_file)

    listed = run("list")
    _assert_capture_is_live(listed.out, key_id, "the list table")
    assert len(KEY_RE.findall(listed.out + listed.err)) == 0

    shown = run("show", key_id)
    _assert_capture_is_live(shown.out, "alice", "the show block")
    assert len(KEY_RE.findall(shown.out + shown.err)) == 0

    as_json = run("list", "--json")
    _assert_capture_is_live(as_json.out, key_id, "list --json")
    assert len(KEY_RE.findall(as_json.out + as_json.err)) == 0


def test_the_created_key_is_absent_from_the_key_files_raw_bytes(run, keys_file):
    """Criterion 6, asserted through the CLI rather than through the writer.

    The file holds a SHA-256 digest. A key present in these bytes would mean the
    whole rotate-rather-than-recover rule was decoration.
    """
    result = run("create", "alice")
    key = KEY_RE.search(result.err).group(0)
    raw = keys_file.read_bytes()
    _assert_capture_is_live(raw.decode(), "alice", "the key file")
    assert key.encode() not in raw
    assert len(KEY_RE.findall(raw.decode())) == 0
    # The digest IS there, which is what makes the absence above meaningful
    # rather than a sign nothing was written.
    assert keys_mod.digest(key).encode() in raw


@pytest.fixture
def loose_umask():
    """0o022, restored. Criterion 17 must not pass on a developer's tight umask.

    `os.umask` is process-global, so leaving it set would change how every later
    test in the session creates files.
    """
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_out_file_is_created_0600(run, keys_file, tmp_path, loose_umask):
    """`--out` is the one path that writes a plaintext key to disk.

    Under the common 0022 umask a plain `open(path, "w")` writes a 0644 file
    holding a live key, while every other display-once test in this file still
    passes. The mode has to be set AT CREATION, which is what `os.open(..., 0o600)`
    does and what an `open()` followed by a `chmod` does not: between the two
    calls the file is readable.
    """
    out = tmp_path / "issued.key"
    log_path = tmp_path / "run.log"
    result = run("create", "alice", "--out", str(out), "--log-file", str(log_path))
    assert result.code == 0

    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert len(KEY_RE.findall(out.read_text())) == 1
    assert len(KEY_RE.findall(result.out)) == 0

    log_text = log_path.read_text()
    _assert_capture_is_live(log_text, "Created key", "the --log-file file")
    assert len(KEY_RE.findall(log_text)) == 0


def test_out_refuses_an_existing_path(run, keys_file, tmp_path):
    """O_EXCL, not truncate. Overwriting destroys the file and can reuse its mode."""
    out = tmp_path / "issued.key"
    out.write_text("something already here\n")
    result = run("create", "alice", "--out", str(out))
    assert result.code == 2
    assert out.read_text() == "something already here\n"


# ---------------------------------------------------------------------------
# Criterion 9 and 11 -- no store, no usage file
# ---------------------------------------------------------------------------


def test_no_keys_verb_opens_the_store_database(run, keys_file, monkeypatch):
    """Every verb runs on a machine that has never built an index.

    `_open_store` (`kb/cmds/_common.py:104-111`) constructs a `SqliteStore`, runs
    `check_schema` and registers the store for observability. None of the three
    exists on a fresh install, and `kb keys list` is the first command an
    operator runs after a refusal.

    The verbs come from the parser's `choices=`, never from a literal count. When
    S4.5.4 adds `usage` in phase 3 this test covers it on the day it lands; a
    hard-coded seven would keep passing over the seven it named.
    """
    from contextlake.kb.cmds import _common

    # A RECORDER, not just a raiser. A patched `_open_store` that only raises is
    # not detectable from the exit code: `cli.py`'s top-level kb guard catches
    # every exception, logs it and exits 1, and exit 1 is also what a legitimate
    # unknown id returns. A test reading the exit code alone therefore passes on
    # a `list` that opens the database, which is the exact break this test has to
    # catch. The flag is the evidence; the exit code is not.
    opened = []

    def _refuse(*args, **kwargs):
        opened.append(True)
        raise AssertionError("a kb keys verb opened the store database")

    monkeypatch.setattr(_common, "_open_store", _refuse)

    run("create", "seed-key")
    verbs = _verbs()
    assert len(verbs) >= 7
    for verb in verbs:
        # Exit codes are not asserted per verb: an unknown id exits 1 and a
        # missing --before exits 2, both legitimately. What IS asserted is that
        # `_open_store` was never reached, and that the run produced one of the
        # command's own exit codes rather than crashing.
        result = run(*_args_for(verb), stdin="ctxlake_bogus")
        assert not opened, f"{verb} opened the store database"
        assert result.code in (0, 1, 2), verb


def test_keys_list_works_with_no_usage_file_and_no_store(run, keys_file, tmp_path):
    """Criterion 11, and the FIRST of the column's three states.

    With no usage file at this store every LAST USED cell reads `-`, NOT
    `never`. The two are different claims: `-` says nothing measured this, and
    `never` says the record exists and holds no call for this key. One value
    for both is what has an operator revoke a key that was used seconds ago.
    The other two states are in `test_keys_list_last_used_three_states`.

    It also holds the other half of criterion 11: a full table and exit 0 on a
    machine with no store built, because this reads the usage file by path and
    opens no database.
    """
    run("create", "alice")
    run("create", "bob")
    result = run("list")
    assert result.code == 0

    rows = [line for line in result.out.splitlines() if "k_" in line]
    assert len(rows) == 2
    header = next(line for line in result.out.splitlines() if "LAST USED" in line)
    column_at = header.index("LAST USED")
    for row in rows:
        assert row[column_at:].strip() == "-"
    # The cell alone cannot say why, so the note carries it. Without this the
    # placeholder is just an ambiguous dash.
    assert "no usage file at this store" in result.out


# ---------------------------------------------------------------------------
# Criterion 10 -- the three permission cases
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not keyfile.POSIX, reason="mode bits are a POSIX question")
def test_group_bits_do_not_stop_keys_list(run, keys_file):
    """A policy refusal, and `list` is the command used to diagnose it.

    Blocking the operator from seeing what exists is the wrong failure. `list`
    warns and exits 0; a WRITE verb refuses.
    """
    run("create", "alice")
    keys_file.chmod(0o640)

    result = run("list")
    assert result.code == 0
    assert _warning_lines(result.out) == 1
    assert "k_" in result.out

    assert run("create", "bob").code == 1


@pytest.mark.skipif(not keyfile.POSIX, reason="mode bits are a POSIX question")
def test_a_world_writable_parent_does_not_stop_keys_list(run, keys_file):
    """The parent-directory mask is the THIRD permission case, not the second.

    A directory anyone can write to is a directory anyone can replace the key
    file inside, so the file's own 0600 proves nothing. The refusal names the
    DIRECTORY, because chmod-ing the file is the fix that does not work.
    """
    run("create", "alice")
    keys_file.parent.chmod(0o777)
    try:
        result = run("list")
        assert result.code == 0
        assert _warning_lines(result.out) == 1

        refused = run("create", "bob")
        assert refused.code == 1
        assert str(keys_file.parent) in refused.out
    finally:
        keys_file.parent.chmod(0o700)


@pytest.mark.skipif(not keyfile.POSIX, reason="mode bits are a POSIX question")
def test_a_fixture_failing_both_masks_prints_two_warning_lines(run, keys_file):
    """Two faults, two lines. One line would mean the operator fixes one and
    hits the other on the next run, which is the failure the report shape exists
    to avoid: `permission_report` reports every failing mask, not the first."""
    run("create", "alice")
    keys_file.chmod(0o640)
    keys_file.parent.chmod(0o777)
    try:
        result = run("list")
        assert result.code == 0
        assert _warning_lines(result.out) == 2
    finally:
        keys_file.parent.chmod(0o700)


# `style.warn` renders its marker as U+26A0, not the word "warning", so counting
# on the word finds zero lines on output that is full of them. Counting the
# marker is what makes "one line per fault" measurable at all.
_WARN_MARK = "\u26a0"


def _warning_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if _WARN_MARK in line)


@pytest.mark.skipif(not keyfile.POSIX, reason="mode bits are a POSIX question")
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file regardless")
def test_an_unreadable_key_file_exits_1_on_every_verb(run, keys_file):
    """An `OSError`, not a policy refusal. Nothing to print, so every verb fails.

    This is the row an implementer collapses into the permission refusal above.
    The two behave differently on purpose: a mask fault means the file was read
    and its mode is wrong, and an unreadable file means nothing was read at all,
    so `list` has nothing to show and exiting 0 would report an empty keyring
    where there is a full one.

    Every verb from the parser's `choices=`, so phase 3's eighth verb is covered
    on the day it lands.
    """
    run("create", "alice")
    keys_file.chmod(0o000)
    try:
        for verb in _verbs():
            result = run(*_args_for(verb), stdin="ctxlake_bogus")
            assert result.code == 1, verb
            assert str(keys_file) in result.out, verb
    finally:
        keys_file.chmod(0o600)


# ---------------------------------------------------------------------------
# Criterion 12 -- what list hides
# ---------------------------------------------------------------------------


def test_list_hides_tombstones_until_all(run, keys_file):
    """Row counts both ways, plus the summary line that names what is hidden.

    A default that hides revoked keys without saying how many it hid is a list
    the operator cannot trust, so the count is asserted alongside the rows.
    """
    run("create", "alice")
    run("create", "bob")
    revoked_id = _only_id(keys_file)
    assert run("revoke", revoked_id).code == 0

    default = run("list")
    assert _row_count(default.out) == 1
    assert revoked_id not in default.out
    assert "1 live key(s). 1 revoked, 0 expired (--all to show)." in default.out

    everything = run("list", "--all")
    assert _row_count(everything.out) == 2
    assert revoked_id in everything.out


def _row_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if "k_" in line and "LAST USED" not in line)


# ---------------------------------------------------------------------------
# Criteria 13 and 14 -- what check says, and the client blocks
# ---------------------------------------------------------------------------


def test_check_says_it_made_no_request_and_does_not_claim_the_scope_was_verified(
    run, keys_file
):
    """`check` has no store and sends nothing. It renders the record's own policy.

    Saying "scope verified" would be a claim about what a server would allow,
    which this command has no way to know: it opened a file.
    """
    created = run("create", "alice", "--tools", "read")
    key = KEY_RE.search(created.err).group(0)

    result = run("check", stdin=key)
    assert result.code == 0
    assert "Valid" in result.out
    assert "No request was made" in result.out
    assert "verified" not in result.out.lower()


def test_check_exits_1_for_malformed_unknown_revoked_and_expired(run, keys_file):
    """Four classes, four exit-1 rows, and the class named in each message."""
    created = run("create", "alice")
    key = KEY_RE.search(created.err).group(0)

    assert run("check", stdin="not-a-key").code == 1
    assert "malformed" in run("check", stdin="not-a-key").out

    other = keys_mod.mint()[0]
    result = run("check", stdin=other)
    assert result.code == 1
    assert "unknown" in result.out

    run("revoke", _only_id(keys_file))
    result = run("check", stdin=key)
    assert result.code == 1
    assert "revoked" in result.out


# The marker that only THIS client's block carries. `Authorization` and `Bearer`
# appear in all five, so on their own they cannot tell "the right block rendered"
# from "some block rendered" -- and rendering the wrong block is a failure mode
# this command actually had: before `--client` carried `choices=`, every
# unrecognised value fell through `_client_block`'s unguarded final `return` and
# printed Zed's settings.json.
_CLIENT_MARKER = {
    "claude-code": "claude mcp add",
    "cursor": "Cursor's mcp.json",
    "vscode": '"inputs"',
    "windsurf": '"serverUrl"',
    "zed": '"context_servers"',
}


@pytest.mark.parametrize("client", keys_cmd.CLIENTS)
def test_each_client_block_names_authorization_and_bearer(run, keys_file, client):
    """Five blocks, each carrying the header field the client actually reads.

    A `Bearer` line in a file the client will not read it from is unsatisfiable,
    not merely unverified, so each of the five was checked against that client's
    own current documentation before its block was written (the URLs are beside
    each block in `keys_cmd._client_block`).

    The block interpolates a VARIABLE rather than inlining the key, which is what
    the zero key-regex match asserts. Zed is the documented exception: its
    `context_servers` reads the header literally and its documentation shows no
    `${env:}` expansion, so its block carries a placeholder and says to keep the
    file private. That is a deviation from T4.2.5.5's "each interpolating a
    variable" bullet, recorded rather than faked.
    """
    result = run("create", f"holder-{client}", "--client", client)
    assert result.code == 0
    assert "Authorization" in result.out
    assert "Bearer" in result.out
    assert len(KEY_RE.findall(result.out)) == 0
    # THIS client's block, not merely a block. Without this the two assertions
    # above pass on any of the five, so a dispatch that answered every --client
    # with the same block would stay green.
    assert _CLIENT_MARKER[client] in result.out
    for other, marker in _CLIENT_MARKER.items():
        if other != client:
            assert marker not in result.out, f"{client} rendered {other}'s block"


@pytest.mark.parametrize("client", [c for c in keys_cmd.CLIENTS if c != "zed"])
def test_every_client_block_but_zed_interpolates_a_variable(run, keys_file, client):
    """The key must not be pasted into a config file the operator will commit.

    Zed is excluded and the exclusion is the finding: zed.dev/docs/ai/mcp (read
    2026-09-05) shows `headers` on a remote server and documents no environment
    interpolation for it, so there is no variable form to write.
    """
    result = run("create", f"holder-{client}", "--client", client)
    assert "${" in result.out


def test_the_vscode_block_says_why_it_is_the_best_secret_handling(run, keys_file):
    """VS Code stores the value outside the config file. That is worth one line."""
    result = run("create", "alice", "--client", "vscode")
    assert "password" in result.out
    assert "outside the config" in result.out


def test_a_client_block_uses_the_url_it_was_given(run, keys_file):
    """`--url` reaches the snippet, so the operator pastes a working config."""
    result = run("create", "alice", "--client", "cursor", "--url",
                 "http://10.0.0.4:8765/mcp")
    assert "http://10.0.0.4:8765/mcp" in result.out


# ---------------------------------------------------------------------------
# The policy block round-trips
# ---------------------------------------------------------------------------


def test_the_policy_flags_are_stored_and_rendered_back(run, keys_file):
    """The acceptance sentence for the six flags, end to end.

    They are parsed into the record's policy block and rendered back. What they
    MEAN is the access-control area's and the rate-limit area's; nothing here
    enforces them.
    """
    result = run("create", "alice", "--tools", "read", "--repos", "acme/*",
                 "--owners", "pseudonymous", "--rate", "60/min", "--burst", "20",
                 "--cost-budget", "30s/min", "--external")
    assert result.code == 0

    stored = json.loads(keys_file.read_text())["keys"][0]["policy"]
    assert stored == {"tools": "read", "repos": "acme/*", "owners": "pseudonymous",
                      "rate": "60/min", "burst": "20", "cost_budget": "30s/min",
                      "external": True}

    shown = run("show", _only_id(keys_file))
    for value in ("read", "acme/*", "pseudonymous", "60/min", "20", "30s/min"):
        assert value in shown.out, value


def test_a_bare_create_stores_an_empty_policy(run, keys_file):
    """The discriminator for `default=argparse.SUPPRESS`.

    If those flags defaulted to a sentinel that landed on the namespace instead
    of being suppressed, `_policy`'s `value not in (None, "")` test would let the
    sentinel through and write it into the key file, where `show` would render it
    back as a real scope. An empty dict is what proves the suppression works.
    """
    assert run("create", "alice").code == 0
    assert json.loads(keys_file.read_text())["keys"][0]["policy"] == {}


def test_a_quota_flag_is_refused_at_create_and_the_good_one_is_stored_as_typed(
        run, keys_file):
    """The defect: a garbage rate minted onto a key that then reads as limited.

    This test used to assert the OPPOSITE -- that `--rate not-a-rate` was
    accepted -- because there was no parser to validate with. `parse_rate` now
    exists, so a typo is refused at the flag the way `--tools` already is, and a
    key can no longer be created carrying a rate the server will not honour.

    The second half is the one a validator can break by over-reaching: the
    ACCEPTED value is stored as the operator typed it, so `60/min` comes back out
    of `kb keys show` spelled the way it went in and they can grep their own
    config for it.
    """
    refused = run("create", "alice", "--rate", "not-a-rate")
    assert refused.code != 0, refused.out + refused.err
    assert "not-a-rate" in refused.out + refused.err, (
        "the refusal does not name the offending string, so the operator "
        "cannot see which value to correct")
    # The file may not exist at all, which is the strongest form of "nothing was
    # minted": the refusal happens before the key file is created.
    assert not keys_file.exists() or not json.loads(keys_file.read_text())["keys"], (
        "a key was minted despite the refusal")

    assert run("create", "alice", "--rate", "60/min").code == 0
    assert json.loads(keys_file.read_text())["keys"][0]["policy"]["rate"] == "60/min"


def test_a_burst_with_no_rate_is_refused_at_create(run, keys_file):
    """The defect: `--burst 20` alone reads like a bound and binds nothing.

    A burst is the request bucket's capacity, and there is no request bucket
    without a rate. Stored alone it renders in `show` and limits nothing, which
    is the shape every label in this module exists to prevent.
    """
    refused = run("create", "alice", "--burst", "20")
    assert refused.code != 0, refused.out + refused.err
    assert "--rate" in refused.out + refused.err, (
        "the refusal does not name the flag that would make the burst mean "
        f"something: {refused.out + refused.err!r}")
    assert not keys_file.exists() or not json.loads(keys_file.read_text())["keys"]


# ---------------------------------------------------------------------------
# rotate and prune
# ---------------------------------------------------------------------------


def test_rotate_prints_a_new_key_on_stderr_and_keeps_both_working(run, keys_file):
    """The handover. Both keys work until the old one's shortened expiry."""
    created = run("create", "alice")
    old_key = KEY_RE.search(created.err).group(0)
    old_id = _only_id(keys_file)

    result = run("rotate", old_id, "--overlap", "24h")
    assert result.code == 0
    assert len(KEY_RE.findall(result.err)) == 1
    assert len(KEY_RE.findall(result.out)) == 0
    new_key = KEY_RE.search(result.err).group(0)
    assert new_key != old_key

    assert run("check", stdin=old_key).code == 0
    assert run("check", stdin=new_key).code == 0


def test_prune_refuses_without_before_and_never_removes_a_live_record(run, keys_file):
    """It deletes permanently, so the cutoff is typed and a live key is exempt."""
    run("create", "alice")
    assert run("prune").code == 2

    result = run("prune", "--before", "2099-01-01")
    assert result.code == 0
    assert "Pruned 0 record(s)" in result.out
    assert len(json.loads(keys_file.read_text())["keys"]) == 1


# ---------------------------------------------------------------------------
# Part of the policy is enforced and part is not, and every surface says which
# ---------------------------------------------------------------------------
#
# MEASURED TWICE, because a wording change with no measurement behind it is a
# preference. Both runs used a key created `--tools none --repos
# nothing-matches/*`, presented to a live `kb serve --transport http
# --keys-only` server over HTTP.
#
# 2026-09-05, before the tools axis was read: `tools/list` answered with all 23
# registered tools and `graph_stats` then ran and returned a result. So the CLI
# told an operator their key was scoped while the server handed it everything.
#
# 2026-09-06, after: `tools/list` answered with an empty list and `graph_stats`
# was refused, naming the group that would grant it. The `repos` half is
# unchanged and still binds nothing.
#
# THE LABEL IS THEREFORE PER AXIS. One label after all three scope axes claims
# the same thing about all three, so enforcing `tools` alone with the old line
# would have said `repos` and `owners` were live too, which is the same class of
# false claim in the other direction.
#
# The assertions below anchor to the SCOPE LINE, never to the note alone. The
# version before this one asserted both `_LABEL` and `_NOTE` against
# `result.out + result.err`, and `_enforcement_note()`'s own first line
# contained both strings, so the note satisfied both assertions on every verb
# and the bracketed per-axis label was never independently checked. A test that
# cannot see the thing it claims to pin is the defect it exists to catch.

_LABEL = "recorded, not enforced"
_ENFORCED_LABEL = "(enforced)"
_NOTE = "This release enforces"


def _scope_line_of(text: str) -> str:
    """The rendered scope line, so an assertion cannot be satisfied by the note.

    Returns "" when no surface in `text` rendered one, which is what lets the
    walk below tell "this verb renders no policy" from "this verb renders one
    with no label".
    """
    for line in text.splitlines():
        if "tools=" in line:
            return line
    return ""


def _policy_argv(verb: str, key_id: str) -> list[str]:
    return {"create": ["create", "bob", "--tools", "read", "--repos", "acme/*"],
            "list": ["list"],
            "show": ["show", key_id],
            "check": ["check"],
            "revoke": ["revoke", key_id],
            "rotate": ["rotate", key_id],
            "prune": ["prune", "--before", "2020-01-01"],
            # `usage` renders no policy, so the walk below skips it on its own
            # output. It is listed here so the verb reaches the handler rather
            # than dying at argparse, which would skip it for the wrong reason.
            "usage": ["usage"]}[verb]


def test_every_verb_that_renders_a_policy_labels_each_axis(run, keys_file):
    """Driven by the parser's own verb list, so an eighth verb is covered too.

    Three halves, and the third is the one the previous version could not see:
    the ENFORCED marker on the enforced axis, the NOT-ENFORCED marker on an
    unenforced one, and the note that says what the markers mean. Asserting only
    the not-enforced marker passes on a blanket label that lies about `tools`;
    asserting only the enforced one passes on a blanket label that lies about
    `repos`. Both markers on ONE rendered line is what pins the split.

    The key file is rebuilt before every verb so the mutating verbs cannot
    change what a later verb sees. `revoke` sorts before `check` in the
    parser's `choices=`, and a shared record would leave `check` reporting a
    revoked key with no scope block at all, which is a pass this test has not
    earned.
    """
    rendered = []
    for verb in _verbs():
        keys_file.unlink(missing_ok=True)
        created = run("create", "alice", "--tools", "read", "--repos", "acme/*",
                      "--rate", "60/min")
        key = KEY_RE.search(created.err).group(0)
        key_id = _only_id(keys_file)

        result = run(*_policy_argv(verb, key_id),
                     stdin=key if verb == "check" else "")
        text = result.out + result.err
        if "tools=" not in text and "60/min" not in text and "acme/*" not in text:
            continue
        rendered.append(verb)
        assert _NOTE in text, (
            f"`kb keys {verb}` renders a policy and never says which axes are "
            "enforced")
        if verb == "list":
            # The table has no room for a bracketed marker, so the note is the
            # only place the split can reach that surface. As of 9.3.0 every axis
            # is enforced, so there is nothing for that clause to name and it is
            # correctly absent -- printing it with an empty list is the defect
            # `test_the_note_omits_the_unenforced_clause_when_there_is_none`
            # pins. The clause's presence when an axis IS unenforced is covered
            # by the same test, which forces that state.
            from contextlake.kb.cmds.keys_cmd import _unenforced_axes

            if _unenforced_axes():
                assert _LABEL in text, (
                    "`kb keys list` lost the not-enforced clause while axes are "
                    "still unenforced")
            continue
        line = _scope_line_of(text)
        assert line, f"`kb keys {verb}` renders a policy with no scope line"
        assert f"tools=read  {_ENFORCED_LABEL}" in line, (
            f"`kb keys {verb}` renders an ENFORCED tools axis with no "
            f"{_ENFORCED_LABEL!r} beside it, so an operator reads a live "
            f"restriction as inert and hands the key out: {line!r}")
        # `repos` became ENFORCED in 9.3.0, so this line asserts the enforced
        # marker where it used to assert the other one. The not-enforced half of
        # the split is kept live by
        # `test_the_not_enforced_marker_still_works_when_an_axis_is_not`, which
        # forces a divergence rather than relying on one existing: every axis is
        # enforced today, so an assertion that merely looked for the marker
        # somewhere would pass on a build that had lost the machinery entirely.
        assert f"repos=acme/*  {_ENFORCED_LABEL}" in line, (
            f"`kb keys {verb}` renders an ENFORCED repos axis with no "
            f"{_ENFORCED_LABEL!r} beside it: {line!r}")

    assert sorted(rendered) == ["check", "create", "list", "show"], rendered


def test_the_note_omits_the_unenforced_clause_when_there_is_none(
        run, keys_file, monkeypatch):
    """The clause must not render with an empty list.

    Verbatim, on a real `kb keys create` run at the moment `repos` became enforced:
    `These are recorded, not enforced: . So a key reads every indexed repository,
    whatever those say.` -- an empty list followed by a sentence telling the operator
    their key is unscoped, printed on a run where the key WAS scoped.

    Every existing test asserted the phrase was PRESENT, so none of them could see
    it. Found by minting a key and reading the output.
    """
    from contextlake.kb.cmds.keys_cmd import _unenforced_axes

    # Today: nothing unenforced, so the clause is absent and no empty list is printed.
    assert not _unenforced_axes()
    created = run("create", "alice", "--tools", "read", "--repos", "acme/*")
    text = created.out + created.err
    assert f"{_LABEL}:" not in text, (
        "the not-enforced clause rendered with nothing to name: " +
        next((ln for ln in text.splitlines() if _LABEL in ln), ""))
    # The enforced half still renders, so the note did not vanish entirely.
    assert _NOTE in text

    # And with an axis genuinely unenforced the clause comes back, naming it.
    from contextlake.kb import grants

    monkeypatch.setattr(
        grants, "ENFORCED_AXES",
        tuple(a for a in grants.ENFORCED_AXES if a != "repos"))
    keys_file.unlink(missing_ok=True)
    text = (lambda r: r.out + r.err)(run("create", "bob", "--tools", "read"))
    assert f"{_LABEL}: repos" in text, (
        "with `repos` unenforced the clause did not name it, so the note is not "
        "derived from ENFORCED_AXES")


def test_the_not_enforced_marker_still_works_when_an_axis_is_not(
        run, keys_file, monkeypatch):
    """The not-enforced label, exercised by FORCING an unenforced axis.

    Every axis in `_ALL_AXES` is enforced as of 9.3.0, so there is no longer a
    natural example to point at. Deleting the assertion would leave the label's
    machinery untested and a later axis would ship claiming enforcement it does
    not have -- which is the exact failure this family of tests was built for.

    So the divergence is manufactured: drop `repos` from `ENFORCED_AXES` and
    require the renderer to notice. If this fails while the test above passes,
    the renderer has stopped reading `ENFORCED_AXES` and is hardcoding the
    marker.
    """
    from contextlake.kb import grants

    monkeypatch.setattr(
        grants, "ENFORCED_AXES",
        tuple(a for a in grants.ENFORCED_AXES if a != "repos"))

    keys_file.unlink(missing_ok=True)
    created = run("create", "alice", "--tools", "read", "--repos", "acme/*")
    line = _scope_line_of(created.out + created.err)
    assert f"repos=acme/*  ({_LABEL})" in line, (
        f"with `repos` removed from ENFORCED_AXES the renderer still called it "
        f"enforced, so the marker is not derived from that tuple: {line!r}")
    # And the axis that IS still enforced keeps its own marker, so this proves a
    # per-axis split rather than a blanket flip.
    assert f"tools=read  {_ENFORCED_LABEL}" in line, line


def test_an_unknown_tool_group_is_refused_at_create(run, keys_file):
    """A typo must not be minted onto a key that then reads as scoped.

    `--tools grpah` stored the string verbatim and rendered it back, so
    `kb keys show` printed a scope the server would expand to nothing. The key
    then failed every call and the operator's only clue was a value that looked
    right on the CLI.

    Refused at CREATE and not at the request. The server does the opposite with
    the same value in a hand-edited key file -- it denies rather than refusing --
    because refusing there would answer a call the operator meant to narrow.
    That asymmetry is deliberate and it is tested on the server side.
    """
    result = run("create", "alice", "--tools", "grpah")
    assert result.code == 2, result.out + result.err
    text = result.out + result.err
    assert "grpah" in text and "graph" in text, text
    assert not keys_file.exists() or not json.loads(keys_file.read_text())["keys"], (
        "the key was minted anyway, so the refusal is cosmetic")

    assert run("create", "alice", "--tools", "graph").code == 0


@pytest.mark.parametrize("flag", ["tools", "repos"])
def test_an_empty_scope_value_is_refused_rather_than_dropped(run, keys_file, flag):
    """`--tools ""` and an unset `--tools` were indistinguishable in the record.

    `_policy` drops a value that is `None` or `""`, so both stored nothing, and
    `_scope_line` renders a missing axis as `unset`. Those are opposite
    instructions now that the axis is live: unset grants every tool, and the
    operator typing an empty string was narrowing.

    The refusal is at the flag rather than by storing `""`, because a stored
    empty string would render as `unset` while meaning deny, which is the same
    collapse one layer down.
    """
    result = run("create", "alice", f"--{flag}", "")
    assert result.code == 2, result.out + result.err
    assert "empty value" in (result.out + result.err)


def _limits_line_of(text: str) -> str:
    for line in text.splitlines():
        if "rate=" in line:
            return line
    return ""


def test_the_limits_line_names_the_tier_each_value_came_from(run, keys_file,
                                                             tmp_path):
    """The defect: a key limited by a server default that reads as `unset`.

    This test used to assert the opposite -- that the whole line carried
    `(recorded, not enforced)` -- because nothing read those axes. They are read
    now, and the state that replaces the old lie is the MIDDLE one: a key that
    names no rate can still be limited by `[serve] default_rate`, and an
    operator who reads a bare `unset` beside it hands that key out believing it
    is unlimited.

    All four states, as full fragments, because three of them can be produced by
    a renderer that never looks at the config at all.
    """
    # 1. named on the key.
    run("create", "alice", "--rate", "60/min")
    line = _limits_line_of(run("show", _only_id(keys_file)).out)
    assert f"rate=60/min  ({_ENFORCED_LABEL[1:-1]})" in line, line

    # 2. nothing anywhere: unset AND unlimited.
    assert "cost_budget=unset  (no limit)" in line, line

    # 3. typed `none` on the key: the operator's own opt-out, which beats a
    #    server default and must not render as `unset`.
    keys_file.unlink(missing_ok=True)
    run("create", "bob", "--rate", "none")
    line = _limits_line_of(run("show", _only_id(keys_file)).out)
    assert "rate=none  (enforced: no limit, set on the key)" in line, line

    # 4. THE ONE THIS TEST EXISTS FOR: inherited from [serve].
    config = tmp_path / "kb.toml"
    config.write_text('[serve]\ndefault_rate = "5/sec"\n')
    keys_file.unlink(missing_ok=True)
    run("--config", str(config), "create", "carol")
    line = _limits_line_of(run("--config", str(config), "show",
                               _only_id(keys_file)).out)
    assert "rate=unset -> 5/sec from [serve] default_rate  (enforced)" in line, (
        f"a key inheriting the server default renders as though nothing limits "
        f"it: {line!r}")


def test_a_burst_beside_no_rate_is_not_reported_as_enforced(run, keys_file):
    """The defect: a flat membership test calling an inert axis enforced.

    A burst is the request bucket's capacity and there is no request bucket
    without a rate, so a burst recorded alone binds nothing. Listed in
    `enforced_axes` it would make `policy_enforced` True for a key that limits
    nothing, which is the label lying in the direction this work exists to stop.

    `--burst` alone is refused at CREATE, so the record is written by hand here.
    That is the only way this state reaches a real file, and it is why the guard
    is in `grants.enforced_axes` and not only at the flag.
    """
    from contextlake.kb import grants

    assert grants.enforced_axes({"burst": "20"}) == [], (
        "a burst with no rate is reported as enforced")
    assert grants.policy_is_enforced({"burst": "20"}) is False
    assert grants.enforced_axes({"rate": "none", "burst": "20"}) == ["rate"], (
        "`rate = none` is no rate at all, so the burst beside it still binds "
        "nothing")
    # The positive control: beside a real rate the burst IS enforced, so the
    # assertions above are not passing for a function that drops burst always.
    assert grants.enforced_axes({"rate": "60/min", "burst": "20"}) == [
        "rate", "burst"]


def test_every_verb_carries_the_resolved_quota_fields(run, keys_file):
    """The four public `--json` fields, on every per-record document.

    `policy` says what is written on the key; these say what a running server
    will apply. A consumer that reads only `policy.rate` renders a key
    inheriting `[serve] default_rate` as unlimited.
    """
    created = json.loads(run("create", "alice", "--rate", "60/min",
                             "--json").out)
    key_id = created["id"]
    key = KEY_RE.search(run("create", "bob", "--rate", "6/min").err).group(0)
    shown = json.loads(run("show", key_id, "--json").out)
    listed = json.loads(run("list", "--json").out)
    checked = json.loads(run("check", "--json", stdin=key).out)
    rotated = json.loads(run("rotate", key_id, "--json").out)
    revoked = json.loads(run("revoke", rotated["new"]["id"], "--json").out)
    pruned = json.loads(run("prune", "--before", "2020-01-01", "--json").out)

    records = [created, shown, checked, rotated["old"], rotated["new"], revoked,
               listed["keys"][0], *pruned["removed_keys"]]
    assert set(_verbs()) == {"create", "list", "show", "revoke", "rotate",
                             "check", "prune", "usage"}, (
        "a verb was added and this walk did not grow with it")
    # `usage` is named above and absent from the walk on purpose: its document
    # is a summary of recorded calls and carries no key record, so there is no
    # per-record quota block on it to check.
    for document in records:
        for field in ("effective_rate", "effective_burst",
                      "effective_cost_budget", "limits_source"):
            assert field in document, (field, sorted(document))
        assert set(document["limits_source"]) == {"rate", "burst",
                                                  "cost_budget"}
        assert set(document["limits_source"].values()) <= {"key", "config",
                                                           "unset"}
    assert created["effective_rate"] == "60/min"
    assert created["limits_source"]["rate"] == "key"
    # The built-in burst is APPLIED and its tier stays `unset`: nobody chose it.
    assert created["effective_burst"] == "20"
    assert created["limits_source"]["burst"] == "unset"
    assert created["effective_cost_budget"] is None


def test_show_never_claims_the_unenforced_axes_restrict_anything(
        run, keys_file, monkeypatch):
    """The wording gate: no axis may claim a restriction it does not apply.

    Verbatim, at `keys_cmd.py:567` in 8.13.0: "grant expanded at 8.13.0. Tools
    added since are denied; rotate to pick them up." Both halves were false then,
    so the ban cannot be blanket: forbidding the word "denied" outright would
    forbid the correct sentence about an axis that really does deny, and push the
    next reader to weaken the label instead.

    THE DIRECTION FLIPPED FOR `repos` IN 9.3.0. It is enforced now, by
    `ScopedStore`, so `(enforced)` beside it is the true statement and the old
    version of this test forbade it. What the test still pins is that the wording
    tracks the enforcement, which it checks in BOTH directions: enforced axes say
    so, and an axis outside `ENFORCED_AXES` says the opposite. The second half is
    manufactured, because every axis is enforced today and an assertion with
    nothing to point at is not a guard.

    "grant expanded" stays banned outright, unchanged: `_policy` stores the raw
    string and expands nothing at create, and `grants._expand` expands live per
    call, so there is no expansion stamped on a record to talk about.
    """
    run("create", "alice", "--tools", "read", "--repos", "acme/*")
    text = (lambda r: r.out + r.err)(run("show", _only_id(keys_file)))
    _assert_capture_is_live(text, "acme/*", "kb keys show")

    lowered = text.lower()
    assert "grant expanded" not in lowered, (
        "`kb keys show` claims a grant was expanded onto the record. Groups are "
        "expanded per call, so there is no stamped expansion to report")
    scope = _scope_line_of(text).lower()
    repos = scope.split("repos=", 1)[1].split("owners=", 1)[0]
    assert _LABEL not in repos, (
        f"the repos axis is enforced and still carries {_LABEL!r}, so an operator "
        f"reads a live restriction as inert and hands the key out: {repos!r}")
    assert "enforced)" in repos, repos
    assert _NOTE in text

    # The other direction. With `repos` outside ENFORCED_AXES the same line must
    # carry the not-enforced label and must NOT claim a restriction, which is the
    # sentence the original defect shipped.
    from contextlake.kb import grants

    monkeypatch.setattr(
        grants, "ENFORCED_AXES",
        tuple(a for a in grants.ENFORCED_AXES if a != "repos"))
    keys_file.unlink(missing_ok=True)
    run("create", "alice", "--tools", "read", "--repos", "acme/*")
    text = (lambda r: r.out + r.err)(run("show", _only_id(keys_file)))
    repos = _scope_line_of(text).lower().split("repos=", 1)[1].split("owners=", 1)[0]
    for claim in ("enforced)", "may read", "restricted to", "cannot read"):
        assert claim not in repos.replace(f"({_LABEL})", ""), (
            f"with `repos` outside ENFORCED_AXES the axis still claims {claim!r}, "
            f"so the wording is a constant rather than a reading: {repos!r}")


def test_an_unset_scope_axis_does_not_read_as_a_restriction(run, keys_file):
    """A bare create has an empty policy, and `show` used to call that `none`.

    `_scope_line` read `policy.get('tools', 'none')`, so `contextlake kb keys
    create alice` with no flags printed `tools=none  repos=none
    owners=default`. That reads as "no tools and no repositories" for the key
    every operator makes on the default path, and `_row` rendered the identical
    record as `-` in the same release: two functions, one record, opposite
    readings.

    The two renderings are still spelled differently on purpose. `_row` keeps
    `-`, which is the table convention for an empty cell here and costs no
    column width; `_scope_line` spells `unset` because a label block has room
    for a word. Both mean "nothing was recorded", which is the fact. What must
    never come back is `none`, which reads as a denial.
    """
    assert run("create", "alice").code == 0
    assert json.loads(keys_file.read_text())["keys"][0]["policy"] == {}

    shown = run("show", _only_id(keys_file)).out
    _assert_capture_is_live(shown, "scope", "kb keys show")
    assert "tools=unset" in shown and "repos=unset" in shown
    assert "tools=none" not in shown, (
        "an unset tools axis renders as `none`, which reads as `no tools` for a "
        "key that has every tool")
    assert "repos=none" not in shown
    assert "owners=default" not in shown

    listed = run("list").out
    assert " -  " in listed or listed.rstrip().endswith(" -"), listed


def test_the_json_surfaces_carry_the_not_enforced_flag(run, keys_file, monkeypatch):
    """A script reading `policy` gets no label out of a text line.

    `{"tools": "none"}` on its own says the opposite of the truth to anything
    that renders it, so every JSON surface carries the flag as data.

    This walks ALL SEVEN, not the two that shipped it. Any document holding a
    `policy` object needs the flag, and `create`, `rotate`, `revoke`, `prune`
    and `check` all carry one now. When they started emitting JSON, two new
    surfaces would have rendered policy values with no label and this test would
    have stayed green over exactly the defect it names.

    The argv is threaded through the run rather than tabulated, because `rotate`
    changes the id every other verb needs.
    """
    # `repos` IS enforced as of 9.3.0, so this fixture no longer has a naturally
    # unenforced axis to build on -- every axis is enforced. The walk below is about
    # whether EVERY verb carries a DERIVED flag, not about which axes happen to be
    # live, so the divergence is manufactured and the walk is kept intact. Without
    # this the test would have to expect True everywhere, and a build that hardcoded
    # True would pass it.
    from contextlake.kb import grants

    monkeypatch.setattr(
        grants, "ENFORCED_AXES",
        tuple(a for a in grants.ENFORCED_AXES if a != "repos"))

    key = KEY_RE.search(
        run("create", "alice", "--tools", "none",
            "--repos", "nothing-matches/*").err).group(0)
    key_id = _only_id(keys_file)

    # bob records `repos` too, so every one of the seven documents below
    # describes a key with an unenforced axis and every one reads False. Without
    # it `create` alone would read True and the walk would need a per-verb
    # expected value, which is re-deriving the thing being tested.
    created = json.loads(
        run("create", "bob", "--tools", "none",
            "--repos", "nothing-matches/*", "--json").out)
    assert created["policy"] == {"tools": "none", "repos": "nothing-matches/*"}

    shown = json.loads(run("show", key_id, "--json").out)
    assert shown["policy"] == {"tools": "none", "repos": "nothing-matches/*"}

    listed = json.loads(run("list", "--json").out)
    checked = json.loads(run("check", "--json", stdin=key).out)
    assert checked["policy"] == {"tools": "none", "repos": "nothing-matches/*"}

    rotated = json.loads(run("rotate", key_id, "--json").out)
    revoked = json.loads(run("revoke", rotated["new"]["id"], "--json").out)
    pruned = json.loads(run("prune", "--before", "2020-01-01", "--json").out)

    documents = {"create": created, "show": shown, "list": listed,
                 "check": checked, "rotate": rotated, "revoke": revoked,
                 "prune": pruned}
    # `usage` emits a document with no `policy` object in it: it summarises
    # recorded calls, not key records. Named in the subtraction rather than
    # left out of the comparison, so a NINTH verb that does carry a policy
    # still fails here.
    assert set(documents) == set(_verbs()) - {"usage"}, (
        "a verb grew a JSON document and this test did not walk it")
    for verb, document in documents.items():
        # EVERY key in this fixture records `repos`, which nothing enforces, so
        # every document reads False. The value is derived now rather than
        # written as a literal, and the two fixtures below are what make that a
        # measurement instead of a coincidence: one key with only enforced axes
        # reads True, and a key with no axes at all reads False.
        assert document["policy_enforced"] is False, (
            f"`kb keys {verb} --json` claims the policy it renders is enforced "
            "while `repos` is outside ENFORCED_AXES, so the flag is a constant "
            "rather than a reading of the record: a dashboard built on it shows a "
            "scope column that is wrong on every row")
    assert created["enforced_axes"] == ["tools"], created["enforced_axes"]
    assert shown["enforced_axes"] == ["tools"], shown["enforced_axes"]
    assert checked["enforced_axes"] == ["tools"], checked["enforced_axes"]
    assert listed["keys"][0]["enforced_axes"] == ["tools"]


def test_the_enforced_flag_is_derived_from_the_axes_the_key_records(
        run, keys_file, monkeypatch):
    """The flag moves with the key. A literal False could not.

    Three fixtures, because one proves nothing. A key scoped only on enforced
    axes reads True; the same key with one unenforced axis added reads False;
    a key with NO axes reads False, not a vacuous True.

    The empty case is the one worth naming. `all()` over an empty set is True,
    and the fact an operator needs from this field is "is anything limiting this
    key". For `contextlake kb keys create alice` -- the default path, an empty
    policy dict -- the answer is no: it can call every tool. A True there is an
    absent value reading as a pass on the key most operators actually mint.
    """
    keys_file.unlink(missing_ok=True)
    only_enforced = json.loads(
        run("create", "a", "--tools", "read", "--owners", "real", "--json").out)
    assert only_enforced["policy_enforced"] is True
    assert only_enforced["enforced_axes"] == ["tools", "owners"]

    # A key carrying `repos` reads True as of 9.3.0, because the store filter
    # behind it is real. This assertion moved with the enforcement.
    scoped = json.loads(
        run("create", "b", "--tools", "read", "--repos", "acme/*", "--json").out)
    assert scoped["policy_enforced"] is True, scoped["enforced_axes"]
    assert scoped["enforced_axes"] == ["tools", "repos"]

    # The False case now has to be MANUFACTURED, and it still has to exist. Every
    # axis is enforced today, so a fixture that merely looked for a False would have
    # nothing to point at, and deleting it would leave the flag's derivation untested
    # until some later axis shipped claiming enforcement it did not have.
    #
    # Dropping `repos` from the tuple must flip the same key to False. If it does
    # not, the flag is no longer derived from `ENFORCED_AXES` and every surface that
    # renders it is repeating a constant.
    from contextlake.kb import grants

    monkeypatch.setattr(
        grants, "ENFORCED_AXES",
        tuple(a for a in grants.ENFORCED_AXES if a != "repos"))
    keys_file.unlink(missing_ok=True)
    mixed = json.loads(
        run("create", "b", "--tools", "read", "--repos", "acme/*", "--json").out)
    assert mixed["policy_enforced"] is False, (
        "with `repos` removed from ENFORCED_AXES a key recording it still reads "
        "as fully enforced, so the flag is not derived from that tuple")
    assert mixed["enforced_axes"] == ["tools"]
    monkeypatch.undo()
    keys_file.unlink(missing_ok=True)
    run("create", "a", "--tools", "read", "--owners", "real", "--json")

    # A rate IS enforced, so a key carrying only tools and a rate reads True.
    # Without this the test above passes for a build where nothing is enforced.
    quota = json.loads(
        run("create", "d", "--tools", "read", "--rate", "60/min", "--json").out)
    assert quota["policy_enforced"] is True, quota["enforced_axes"]
    assert quota["enforced_axes"] == ["tools", "rate"]

    bare = json.loads(run("create", "c", "--json").out)
    assert bare["policy"] == {}
    assert bare["enforced_axes"] == []
    assert bare["policy_enforced"] is False, (
        "a key with no policy at all reads as enforced. `all()` over nothing is "
        "True and that is the wrong answer here: this key can call every tool")

    # The document-level flag on a COLLECTION is the aggregate, and an empty
    # collection is False for the same reason.
    listed = json.loads(run("list", "--json").out)
    assert listed["policy_enforced"] is False, (
        "one unenforced key in the table and the document still claims the "
        "whole table is enforced")
    empty = json.loads(run("prune", "--before", "2020-01-01", "--json").out)
    assert empty["removed"] == 0
    assert empty["policy_enforced"] is False, (
        "a document that rendered no key at all claims enforcement over nothing")


def test_the_label_is_pinned_to_the_server_that_enforces_it(run, keys_file):
    """The precondition, in the direction that keeps the wording honest.

    Every assertion above is about wording. This one reads the reason the
    wording is right. The version before it asserted the OPPOSITE -- that
    `check_tool_grant` did not exist and `build_http_app` still read
    `grant_source = None` -- and it was written to fail when enforcement landed,
    which it did. This is its replacement, pointed the same way: it fails if the
    CLI's claim and the gate's behaviour come apart in either direction.

    Three couplings, none of them a string match on prose:

    1. `ENFORCED_AXES` is the one list. The CLI reads it, so an axis added there
       with no rule behind it makes every surface claim something the gate does
       not check.
    2. Every axis named in it has a rule that can refuse. Asserted by calling
       the check, not by reading the source: a rule that exists and never denies
       is the write-with-no-consumer shape this whole frame exists to keep out.
    3. Every axis NOT in it is still rendered with the not-enforced marker.
    """
    from contextlake.kb import grants
    from contextlake.kb.server import GrantDenied, Principal

    assert grants.ENFORCED_AXES == ("tools", "repos", "external", "owners",
                                    "rate", "burst", "cost_budget"), (
        "the enforced axes moved. Re-read _enforcement_note and _axis in "
        "kb/cmds/keys_cmd.py: they render every axis from this list")

    principal = Principal("k_test")
    # `rate`, `burst` and `cost_budget` are enforced by the QUOTA, not by
    # `check_tool_grant`: the refusal is a 429 from the gate, above the tool
    # wrapper, so there is no `GrantDenied` to raise here. They are walked in
    # `test_the_quota_axes_refuse_through_the_limiter` instead, which calls the
    # thing that actually enforces them.
    for axis, policy, tool in (
            ("tools", {"tools": "none"}, "graph_stats"),
            ("owners", {"owners": "hidden"}, "who_knows")):
        assert axis in grants.ENFORCED_AXES
        with pytest.raises(GrantDenied):
            grants.check_tool_grant(principal, tool, policy)
        # The positive control. Without it the refusal above passes for a check
        # that denies everything, which is not enforcement either.
        grants.check_tool_grant(principal, tool, {})

    # The QUOTA axes, walked through the thing that enforces them. The gate
    # answers 429 above the tool wrapper, so nothing here raises GrantDenied,
    # and asserting only on `check_tool_grant` would have let the label move
    # with no limiter behind it.
    from contextlake.kb import ratelimit

    limits = ratelimit.resolve_limits(
        {"rate": "2/min", "burst": "4", "cost_budget": "1s/min"},
        ratelimit.ServeDefaults())
    limiter = ratelimit.Limiter(lambda key_id: limits, now=lambda: 0.0)
    for _ in range(4):
        assert limiter.admit("k_test").admitted
    refused = limiter.admit("k_test")
    assert not refused.admitted and refused.which == "requests", refused
    # The positive control: a key with no quota is never refused, so the
    # refusal above is the policy and not a limiter that denies everything.
    open_limiter = ratelimit.Limiter(lambda key_id: ratelimit.UNLIMITED,
                                     now=lambda: 0.0)
    for _ in range(50):
        assert open_limiter.admit("k_test").admitted

    # The STORE axes, walked through the thing that enforces them. `repos` and
    # `external` are not decided by `check_tool_grant` at all: the refusal is a
    # filtered read inside `ScopedStore`, below the tool wrapper, so a key denied a
    # repository gets an empty answer rather than an exception. Asserting only on
    # `check_tool_grant` would let the label move with no filter behind it, which is
    # the same gap the quota block above closes for the rate axes.
    from contextlake.kb.scoped_store import (
        ScopedStore,
        open_request_scope,
        reset_request_scope,
    )

    class _Node:
        def __init__(self, repo):
            self.repo = repo

    class _FakeStore:
        path = "/nowhere"

        def __init__(self):
            self.nodes = {"in": _Node("acme/api"), "out": _Node("other/api")}

        def get_node(self, node_id):
            return self.nodes.get(node_id)

        def list_partitions(self):
            return ["acme/api", "other/api", "(external)"]

    for axis in ("repos", "external"):
        assert axis in grants.ENFORCED_AXES
    scoped = ScopedStore(_FakeStore(),
                         lambda: grants.repo_scope_of({"repos": "acme/*"}))
    token = open_request_scope()
    try:
        assert scoped.get_node("in") is not None
        # The refusal: a node in a repository this key was not granted.
        assert scoped.get_node("out") is None
        # `external` rides alongside and is off unless granted.
        assert "(external)" not in scoped.visible_partitions()
    finally:
        reset_request_scope(token)

    # The positive controls, both directions. Without them the refusals above pass
    # for a filter that denies everything, and for an `external` flag nothing reads.
    unscoped = ScopedStore(_FakeStore(), lambda: ([], False))
    token = open_request_scope()
    try:
        assert unscoped.get_node("out") is not None
    finally:
        reset_request_scope(token)

    with_external = ScopedStore(
        _FakeStore(),
        lambda: grants.repo_scope_of({"repos": "acme/*", "external": True}))
    token = open_request_scope()
    try:
        assert "(external)" in with_external.visible_partitions()
    finally:
        reset_request_scope(token)

    # And the CLI now calls it enforced, because it reads ENFORCED_AXES.
    run("create", "alice", "--repos", "acme/*")
    line = _scope_line_of(run("show", _only_id(keys_file)).out)
    assert f"repos=acme/*  {_ENFORCED_LABEL}" in line, line


def test_keys_check_refuses_a_terminal_instead_of_blocking(run, keys_file,
                                                           monkeypatch):
    """`check` on a terminal hung with no prompt and no output.

    `sys.stdin.read()` blocks until end-of-file. Nothing printed a prompt
    first, so `contextlake kb keys check` typed on its own showed a blank
    screen until the operator found Ctrl-D. Measured on 2026-09-05 with STDIN
    on a real pty: no output and still blocked after 8 seconds.

    `read` is replaced with a stub that RECORDS the call rather than left
    alone. Without it this test would pass on the fixture's empty string even
    after the guard was removed, which is the pass the guard exists to stop.
    The recorded call is asserted before the exit code, so removing the guard
    fails on the sentence that names the defect rather than on `1 == 2`:
    `cli.main` turns any exception out of a handler into exit 1, which would
    read as an unrelated crash.
    """
    reached = []

    def _blocks(self):
        reached.append(True)
        raise AssertionError("read() on a terminal blocks until Ctrl-D")

    monkeypatch.setattr(_Stdin, "isatty", lambda self: True)
    monkeypatch.setattr(_Stdin, "read", _blocks)

    result = run("check")
    assert not reached, (
        "sys.stdin.read() was reached with isatty() true: on a real terminal "
        "that call blocks until end-of-file, with no prompt and no output")
    assert result.code == 2
    text = result.out + result.err
    assert "nothing piped in" in text
    assert 'printf \'%s\' "$KEY" | contextlake kb keys check' in text


def test_the_scope_flag_help_does_not_promise_a_restriction():
    """`--help` is a surface too, and it read as a live grant.

    Verbatim before this change: `--tools` said "the tool groups this key may
    call", `--repos` "the repo globs this key may read", and `--owners` "how
    much author identity this key sees". All three are present tense and all
    three are false. The epilog said "nothing enforces them yet" further down
    the page, which does not help a reader who skims the flag list.
    """
    helps = {a.dest: (a.help or "") for a in _keys_parser()._actions}
    # PER FLAG, because two of the three are enforced now. A blanket assertion
    # either way is a wrong claim about one of them: `--repos` still binds
    # nothing, and `--tools` now refuses a call outside its grant.
    for dest in ("repos",):
        text = helps[dest]
        assert ("nothing enforces it in this release" in text
                or "NOT validated in this release" in text), (
            f"--{dest} does not say its value binds nothing: {text!r}")
    for dest in ("tools", "owners", "rate", "cost_budget"):
        text = helps[dest]
        assert "nothing enforces it in this release" not in text, (
            f"--{dest} is enforced now and its help still says nothing reads "
            f"it, so an operator skips a flag that would have scoped the key: "
            f"{text!r}")
        assert "nforced" in text, (
            f"--{dest} never says its value is enforced: {text!r}")
    # Only `--repos` still has to avoid the present-tense grant wording. For the
    # two enforced flags "may call" is now the accurate description, and banning
    # it there would push the next reader to weaken the help instead.
    for claim in ("may read", "this key sees"):
        assert claim not in helps["repos"], (
            f"--repos help claims {claim!r}, which reads as a live grant")


# ---------------------------------------------------------------------------
# `--json` on all seven verbs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(_verbs()))
def test_every_verb_writes_a_json_document_to_stdout(run, keys_file, verb):
    """The defect this replaced: five verbs took `--json` and printed prose.

    9.0.0 refused the flag on those five, which was honest and not the
    destination. Now all seven answer it.

    The EXIT CODE is deliberately not asserted here. `_args_for` gives `show`,
    `revoke` and `rotate` a name where an id belongs, so those three land on
    `unknown_id` at exit 1, and `check` reads the fixture's empty stdin and
    lands on `malformed`, also 1. The invariant that holds across all seven is
    the one under test: stdout parses, on every exit path.

    `json.loads` over the WHOLE of stdout is what proves stdout carries the
    document alone. One stray log line and it raises.
    """
    result = run(*_args_for(verb), "--json")
    document = json.loads(result.out)
    assert isinstance(document, dict), (
        f"`kb keys {verb} --json` did not write an object to stdout")


@pytest.mark.parametrize("verb", sorted(_verbs()))
def test_no_verb_writes_prose_to_stdout_under_json(run, keys_file, verb):
    """The rule `use_stderr()` exists for, checked on the failure paths too.

    `cmd_keys` hoists `use_stderr()` ahead of dispatch. Before that it was
    called inside `_cmd_list` and `_cmd_show` only, and `_cmd_create` called it
    AFTER its own two refusals, so `create --json --client claude-desktop` would
    have put refusal prose inside a caller's `> out.json` with nothing red.

    `raw_decode` rather than `json.loads`: `loads` raises on a TRAILING line but
    the message names a decode error, and re-serialising to compare would pin
    the key ORDER, which is not the claim. This reads one value and asserts what
    is left is whitespace, so a line printed after the document fails on the
    sentence that says so and a reordered document does not fail at all.
    """
    result = run(*_args_for(verb), "--json")
    _document, end = json.JSONDecoder().raw_decode(result.out.lstrip())
    assert result.out.lstrip()[end:].strip() == "", (
        f"`kb keys {verb} --json` wrote something after the document on stdout")


def test_create_json_refuses_a_client_without_writing_to_stdout(run, keys_file):
    """The refusal `_cmd_create` raised BEFORE it routed `log()` to stderr.

    `--client claude-desktop` is refused at the top of the handler.
    `use_stderr()` used to sit twelve lines below that raise, so the refusal
    sentence went to stdout, into the caller's redirect, and stayed there while
    every existing test passed: none of them combined `--json` with a refusing
    argv.
    """
    result = run("create", "alice", "--client", "claude-desktop", "--json")
    assert result.code == 2
    assert json.loads(result.out) == {
        "error": "refused_client",
        "client": "claude-desktop",
        "clients": list(keys_cmd.CLIENTS),
    }
    assert "is refused" in result.err


def test_the_error_document_carries_a_code_for_every_refusal(run, keys_file):
    """Exit 2 says "malformed request" and nothing else. The code says which.

    A script that asked for machine-readable output and got prose cannot act on
    the result, and that is as true of a failure as of a success. The house
    answer is `{"error": "<snake_case_code>"}` on stdout, which `kb query`,
    `kb owners`, `kb impact` and `kb eval` all already emit.
    """
    cases = {
        ("show",): "missing_argument",
        ("prune",): "missing_argument",
        ("prune", "--before", "not-a-date"): "bad_before",
        ("create", "alice", "--expires", "banana"): "bad_expires",
        ("check", "ctxlake_something"): "key_in_argv",
    }
    # `rotate` reads --overlap and --expires through one `except ValueError`,
    # and they raise the same type, so a bad --expires used to come back coded
    # `bad_overlap` and send the operator to the flag they typed correctly.
    run("create", "alice")
    rotate_id = _only_id(keys_file)
    cases[("rotate", rotate_id, "--overlap", "banana")] = "bad_overlap"
    cases[("rotate", rotate_id, "--expires", "banana")] = "bad_expires"
    for argv, code in cases.items():
        result = run(*argv, "--json")
        assert result.code == 2, argv
        assert json.loads(result.out)["error"] == code, argv


def test_the_key_in_argv_refusal_never_echoes_the_key(run, keys_file):
    """The one place a house error document does NOT echo its target.

    `kb owners --json` returns `{"error": "unknown_repo", "target": ...}`, and
    copying that here would defeat the refusal: argv is refused BECAUSE a key
    there lands in shell history and shows in `ps`, so writing it back into the
    caller's redirect is the same leak by another route.
    """
    secret = "ctxlake_" + "a" * 49
    result = run("check", secret, "--json")
    _assert_capture_is_live(result.out, '"key_in_argv"', "check --json")
    assert secret not in result.out
    assert secret[:16] not in result.out


def test_an_unknown_id_is_an_error_document_at_exit_1(run, keys_file):
    """`show --json k_nope` used to print prose to stdout and emit NO document.

    The not-found branch ran ahead of the `as_json` check in all three verbs.
    Exit 1, not 2: the request was well formed and the id was resolvable in
    principle, which is the same split `kb owners` uses for `unknown_repo`.
    """
    run("create", "alice")
    for verb in ("show", "revoke", "rotate"):
        result = run(verb, "k_nope00", "--json")
        assert result.code == 1, verb
        document = json.loads(result.out)
        assert document["error"] == "unknown_id", verb
        assert document["id"] == "k_nope00", verb
        assert document["keys_file"] == str(keys_file), verb


def test_the_created_key_is_not_in_the_json_document_by_default(run, keys_file):
    """`create --json > provision.json` must not write a live key into that file.

    `_open_key_file` does real work to stop a key landing in a 0644 file: the
    mode is set at creation because the common 0022 umask would otherwise make
    one. A redirect recreates that defect exactly, at the caller's umask, on the
    normal way to consume a `--json` surface. So the key stays on stderr and the
    document says where it went.
    """
    result = run("create", "alice", "--json")
    document = json.loads(result.out)
    _assert_capture_is_live(result.out, '"key_shown_on"', "create --json stdout")
    assert document["key"] is None
    assert document["key_shown_on"] == "stderr"
    assert KEY_RE.findall(result.out) == [], (
        "the plaintext key is in the JSON document on stdout, which is what a "
        "caller redirects into a file at their own umask")
    assert len(KEY_RE.findall(result.err)) == 1


def test_print_key_moves_the_key_from_stderr_into_the_document(run, keys_file):
    """The fd collision, resolved without a new refusal.

    `--print-key` writes the bare key to stdout, and `--json` writes the
    document there. Under `--json` there is one thing on stdout, so the flag
    decides whether the key is INSIDE it instead of beside it, and the two stop
    competing for the same descriptor.

    The first run is the positive control for the second: same command, one flag
    different, and it DOES put a key on stderr. Without it, "no key on stderr"
    would pass over a capture that sees nothing.
    """
    plain = run("create", "alice", "--json")
    assert len(KEY_RE.findall(plain.err)) == 1, (
        "positive control failed: create --json put no key on stderr, so the "
        "stderr assertion below would pass vacuously")

    printed = run("create", "bob", "--json", "--print-key")
    document = json.loads(printed.out)
    assert document["key_shown_on"] == "stdout"
    assert KEY_RE.fullmatch(document["key"] or "")
    assert KEY_RE.findall(printed.err) == [], (
        "--print-key put the key in the document AND on stderr, so the key is "
        "in two places when the flag asked for one")


def test_an_existing_out_path_is_refused_before_the_record_is_minted(run,
                                                                     keys_file,
                                                                     tmp_path):
    """The verified defect: the record was persisted, then `--out` was refused.

    `_save` used to run before `_write_key_file`, so `create --out <existing>`
    minted the key, wrote the record to the key file, hit the O_EXCL refusal and
    exited 2. The record was live in the file, its plaintext was gone forever,
    and the command reported that nothing had worked.

    The key file is asserted BEFORE the exit code. Exit 2 was already the
    behaviour, so asserting it first would let this pass over the defect.
    """
    taken = tmp_path / "already-there.key"
    taken.write_text("another file's contents\n")

    result = run("create", "alice", "--out", str(taken))

    assert not keys_file.exists(), (
        "the key file was written before --out was refused, so a live record "
        "exists whose plaintext was never shown to anybody")
    assert taken.read_text() == "another file's contents\n"
    assert result.code == 2


def test_a_refused_expires_leaves_no_out_file_behind(run, keys_file, tmp_path):
    """The other half of opening `--out` first: tidy up when nothing is minted.

    The descriptor is opened before the key exists, so a later refusal has
    already created an empty file. Leaving it makes the operator's retry fail on
    O_EXCL for a reason their first run caused.
    """
    out = tmp_path / "never-written.key"
    result = run("create", "alice", "--expires", "banana", "--out", str(out))
    assert result.code == 2
    assert not out.exists(), (
        "the --out file was left behind by a create that minted nothing, so the "
        "retry is refused on a file this command made")


def test_revoke_json_separates_revoked_now_from_already_revoked(run, keys_file):
    """Two outcomes, one exit code. `changed` is the only thing that splits them.

    Both branches exit 0 and always have, so an offboarding script could not
    tell "I revoked it" from "somebody else already had". The prose said so and
    nothing else did.
    """
    run("create", "alice")
    key_id = _only_id(keys_file)

    first = json.loads(run("revoke", key_id, "--reason", "left", "--json").out)
    assert first["changed"] is True
    assert first["state"] == "revoked"
    assert first["revoked_reason"] == "left"

    second = json.loads(run("revoke", key_id, "--reason", "again", "--json").out)
    assert second["changed"] is False
    assert second["revoked_at"] == first["revoked_at"], (
        "re-revoking rewrote the timestamp; the FIRST revocation is the audit "
        "answer")
    assert second["revoked_reason"] == "left"


def test_prune_json_reports_what_it_removed(run, keys_file):
    """`prune` exits 0 whether it removed forty records or none.

    A scheduled cleanup logs the count, and parsing it out of "Pruned 2
    record(s) that stopped working before ..." is what the document replaces.
    """
    nothing = json.loads(run("prune", "--before", "2020-01-01", "--json").out)
    assert nothing["removed"] == 0
    assert nothing["removed_keys"] == []
    assert nothing["changed"] is False, (
        "prune reported a write it did not make: the key file is untouched when "
        "nothing matched")

    run("create", "alice")
    key_id = _only_id(keys_file)
    run("revoke", key_id)
    done = json.loads(run("prune", "--before", "2999-01-01", "--json").out)
    assert done["removed"] == 1
    assert [r["id"] for r in done["removed_keys"]] == [key_id]
    assert done["remaining"] == 0
    assert done["changed"] is True


def test_check_json_names_which_of_the_four_failures_it_was(run, keys_file):
    """Malformed, unknown, revoked and expired all exit 1. `reason` splits them.

    A CI gate that warns on `expired` and fails on `revoked` cannot act on the
    exit code, which is the same for all four.
    """
    created = run("create", "alice")
    key = KEY_RE.search(created.err).group(0)
    key_id = _only_id(keys_file)

    valid = json.loads(run("check", "--json", stdin=key).out)
    assert valid["valid"] is True
    assert valid["reason"] is None
    assert valid["id"] == key_id
    assert valid["checked_locally"] is True

    malformed = run("check", "--json", stdin="not-a-key")
    assert malformed.code == 1
    document = json.loads(malformed.out)
    assert document["valid"] is False
    assert document["reason"] == "malformed"
    assert document["id"] is None and document["policy"] is None

    # Minted through `keys_mod` against a throwaway list, so it carries a real
    # checksum and no record in the file holds its digest. A hand-built string
    # of the right LENGTH fails the checksum instead and reads as `malformed`,
    # which would make this branch assert the previous one over again.
    _stray, stray_key = keys_mod.create([], "not-in-the-file")
    unknown = json.loads(run("check", "--json", stdin=stray_key).out)
    assert unknown["reason"] == "unknown"

    run("revoke", key_id)
    revoked = run("check", "--json", stdin=key)
    assert revoked.code == 1
    assert json.loads(revoked.out)["reason"] == "revoked"


def test_a_check_that_could_not_run_is_not_a_check_that_failed(run, keys_file):
    """`valid: false` means the answer is no. It must not mean "I could not ask".

    A key file this account cannot read makes `check` unable to answer, and
    reporting `valid: false` there would tell a caller a live key is bad. The
    error document carries `valid: null`, so the documented idiom holds: test
    `.error` first, and only when it is absent test `.valid`.
    """
    keys_file.write_text("{ this is not json")
    result = run("check", "--json", stdin="ctxlake_" + "0" * 49)
    assert result.code == 1
    document = json.loads(result.out)
    assert document["error"] == "key_file_error"
    assert document["valid"] is None
    assert document["reason"] is None


def test_rotate_json_carries_both_ids_and_the_resolved_overlap(run, keys_file):
    """A rotation script needs both ids to schedule the follow-up revoke.

    `overlap` is the RESOLVED value, not the typed one: the CLI passes no
    default and `keys_mod.DEFAULT_OVERLAP` fills it in, so on the default path
    what the operator typed (nothing) and what was applied (`7d`) differ.
    """
    run("create", "alice")
    old_id = _only_id(keys_file)
    document = json.loads(run("rotate", old_id, "--json").out)

    assert document["old"]["id"] == old_id
    assert document["old"]["rotated_to"] == document["new"]["id"]
    assert document["new"]["rotated_from"] == old_id
    assert document["overlap"] == keys_mod.DEFAULT_OVERLAP
    assert document["overlap_seconds"] == 7 * 24 * 60 * 60
    assert document["changed"] is True
    assert document["key"] is None and document["key_shown_on"] == "stderr"


def test_rotate_honours_out_and_print_key(run, keys_file, tmp_path):
    """Both flags parsed on `rotate` and both were ignored, exiting 0.

    They are declared once on the `keys` parser and the verb is a positional, so
    argparse accepts them on all seven -- the same reason `--json` reached all
    seven. Rotate is where it bit hardest: its new key exists nowhere else and
    there was no machine route to it at all.
    """
    run("create", "alice")
    out = tmp_path / "rotated.key"
    document = json.loads(
        run("rotate", _only_id(keys_file), "--json", "--out", str(out)).out)
    assert document["key_file"] == str(out)
    assert KEY_RE.fullmatch(out.read_text().strip())
    assert stat.S_IMODE(out.stat().st_mode) == 0o600

    printed = run("rotate", document["new"]["id"], "--json", "--print-key")
    assert json.loads(printed.out)["key_shown_on"] == "stdout"
    assert KEY_RE.fullmatch(json.loads(printed.out)["key"] or "")


def test_the_list_document_keeps_every_field_9_0_0_shipped(run, keys_file):
    """`list --json` is a published shape. Adding fields is fine; losing one is not.

    `_row` is a DISPLAY function that feeds `_table`, and its column padding
    leaked into the 9.0.0 document. Those five keys are now frozen display
    strings, not data, and they are the first thing to remove at the next major
    bump. Until then a caller reading `row["tools"] == "-"` keeps working, and
    one written after this reads `row["policy"]` and `row["expires_at"]`.
    """
    run("create", "alice")
    document = json.loads(run("list", "--json").out)
    for field in ("path", "present", "policy_enforced", "live", "revoked",
                  "expired", "keys"):
        assert field in document, field
    row = document["keys"][0]
    for field in ("id", "name", "state", "tools", "repos", "rate", "expires",
                  "last_used"):
        assert field in row, field
    assert row["tools"] == "-" and row["last_used"] == "-"

    assert row["policy"] == {}
    assert row["expires_at"].endswith("Z")
    assert row["last_used_at"] is None
    assert row["last_used_state"] == "not-recorded", (
        "a null last_used_at means `never used` and `nothing measures it` at "
        "once; the sibling is the only thing that separates them")
    assert document["all"] is False
    assert document["permission_ok"] is True
    assert json.loads(run("list", "--all", "--json").out)["all"] is True


def test_the_show_document_keeps_the_digest_and_gains_nothing_secret(run,
                                                                     keys_file):
    """`show --json` shipped `record.to_dict()`, digest included. It stays there.

    The digest is not a credential: the secret is 256 uniform random bits, so a
    SHA-256 of it has nothing to guess. It is still withheld from the text
    surface and from every other JSON surface, because spreading a value one
    surface deliberately hides needs a named reader and there is none.
    """
    created = run("create", "alice")
    key = KEY_RE.search(created.err).group(0)
    key_id = _only_id(keys_file)

    shown = json.loads(run("show", key_id, "--json").out)
    assert len(shown["digest"]) == 64
    assert key not in run("show", key_id, "--json").out

    listed = json.loads(run("list", "--json").out)
    assert "digest" not in listed["keys"][0], (
        "the digest spread from `show` into `list` by inheriting a shared "
        "record builder; no reader asked for it")


def test_the_permission_warnings_reach_a_json_caller(run, keys_file):
    """Under `--json` the warnings go to stderr, which the caller is not reading.

    `kb keys list` is the command an operator runs after a server refuses to
    start, and a bad mask is often why it refused. A document that answers about
    the key file while hiding what is wrong with it answers the wrong question.
    """
    run("create", "alice")
    keys_file.chmod(0o644)
    document = json.loads(run("list", "--json").out)
    assert document["permission_ok"] is False
    assert any("chmod 600" in line for line in document["permission_warnings"])
    assert "0644" in "".join(document["permission_warnings"])


def test_the_client_snippet_is_fields_and_not_only_rendered_lines(run, keys_file):
    """The consumer writes `.vscode/mcp.json` itself. It needs values, not prose.

    `lines` stays, unjoined, for a caller that wants to show the block. Beside
    it are the three values a script actually writes: which header, what to put
    in it, and which file it belongs in.
    """
    document = json.loads(run("create", "alice", "--client", "vscode",
                              "--json").out)
    snippet = document["client_snippet"]
    assert document["client"] == "vscode"
    assert snippet["header_name"] == "Authorization"
    assert snippet["value_template"] == "Bearer ${input:contextlake-key}"
    assert snippet["config_path"] == ".vscode/mcp.json"
    assert snippet["url"] == "http://127.0.0.1:8765/mcp"
    assert isinstance(snippet["lines"], list) and snippet["lines"]
    assert json.loads(run("create", "bob", "--json").out)["client_snippet"] is None


def test_every_client_with_a_block_has_machine_fields_too():
    """A sixth client must not ship a rendered block and no fields.

    `_client_block` branches on the client name with a fallback, so an unlisted
    client would silently render Zed's block. `_CLIENT_FIELDS` is a dict, so the
    same mistake is a KeyError at the point of use -- but only if the two are
    pinned together.
    """
    assert set(keys_cmd._CLIENT_FIELDS) == set(keys_cmd.CLIENTS)
    for client, (header, template, config_path) in keys_cmd._CLIENT_FIELDS.items():
        assert header and template and config_path, client
        assert not KEY_RE.search(template), (
            f"the {client} value template carries something key-shaped; a "
            "template holds a placeholder, never a credential")


# ---------------------------------------------------------------------------
# `kb keys usage` -- the eighth verb, and the LAST USED column it fills
# ---------------------------------------------------------------------------


@pytest.fixture
def usage_store(tmp_path):
    """A config naming a tmp store, and the usage path under it.

    A REAL config, not a patched `_usage_file`. The two paths this verb reads
    come from two different resolvers -- the usage file from the store, the key
    file from `$CONTEXTLAKE_KEYS_FILE` -- and patching the resolver away is how
    a test passes on a build that reads one path for both.
    """
    from contextlake.kb import usage

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return SimpleNamespace(config=str(cfg), path=store_dir / usage.FILENAME)


def _write_usage(usage_store, rows):
    usage_store.path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _row(outcome="ok", *, key="k_1", tool="search_code", ms=10, n=1,
         ts="2026-09-07T10:00Z"):
    return {"ts": ts, "key": key, "tool": tool, "outcome": outcome, "ms": ms,
            "n": n}


def test_usage_sums_n_and_never_counts_lines(run, keys_file, usage_store):
    """One row standing for 500 refused requests reads 1 against a line
    counter, which under-reports a flood by two orders of magnitude."""
    _write_usage(usage_store, [_row("unknown", key=None, tool=None, ms=None,
                                    n=500)])
    result = run("usage", "--config", usage_store.config)
    assert result.code == 0
    assert "500" in result.out
    assert "total" in result.out


def test_usage_percentiles_are_nearest_rank(run, keys_file, usage_store):
    """A mean labelled P50 is a wrong number in a place nobody re-checks: the
    mean of [10,10,10,10,1000] is 208, and neither column may read it."""
    _write_usage(usage_store, [_row(ms=v) for v in (10, 10, 10, 10, 1000)])
    result = run("usage", "--config", usage_store.config)
    assert result.code == 0
    assert "10ms" in result.out and "1000ms" in result.out
    assert "208" not in result.out


def test_usage_prints_a_dash_where_nothing_was_measured(run, keys_file,
                                                        usage_store):
    """`0ms` is a measurement. Printing it where there is none says the calls
    were instant rather than untimed."""
    _write_usage(usage_store, [_row("denied", ms=None)])
    result = run("usage", "--config", usage_store.config)
    assert result.code == 0
    assert "0ms" not in result.out
    header = next(line for line in result.out.splitlines() if "P50" in line)
    row = next(line for line in result.out.splitlines() if "k_1" in line)
    assert row[header.index("P50"):].strip().startswith("-")


def test_usage_key_and_tool_totals_agree(run, keys_file, usage_store):
    """Two views of one number disagreeing is how a report stops being read."""
    _write_usage(usage_store, [_row(key=f"k_{i % 2}", tool=f"t{i % 3}")
                               for i in range(12)])
    result = run("usage", "--config", usage_store.config, "--json")
    doc = json.loads(result.out)
    assert sum(k["calls"] for k in doc["keys"]) == doc["calls"] == 12
    assert sum(t["calls"] for t in doc["tools"]) == doc["calls"]


def test_usage_json_and_text_report_the_same_totals(run, keys_file, usage_store):
    """Two renderings computed twice disagree the first time one is fixed."""
    _write_usage(usage_store, [_row(ms=i) for i in range(7)]
                 + [_row("unknown", key=None, tool=None, ms=None, n=9)])
    doc = json.loads(run("usage", "--config", usage_store.config, "--json").out)
    text = run("usage", "--config", usage_store.config).out
    assert doc["calls"] == 7 and doc["refused_total"] == 9
    assert "7 calls" in text and "9" in text


def test_usage_prints_identity_unset_and_says_what_it_means(run, keys_file,
                                                            usage_store):
    """The fail-closed fault reading as ordinary probing.

    Four things: the class, its count, the total, and the cause line. Without
    the cause line an operator reads it beside `unknown` and `malformed` and
    concludes somebody is scanning them.
    """
    _write_usage(usage_store, [_row("identity_unset", key=None, ms=None, n=12),
                               _row("unknown", key=None, tool=None, ms=None,
                                    n=3)])
    result = run("usage", "--config", usage_store.config)
    assert result.code == 0
    assert "identity_unset" in result.out
    assert "12" in result.out and "15" in result.out
    assert "fault in this server" in result.out


def test_usage_since_filters_rows(run, keys_file, usage_store):
    """A `--since` that filters nothing. The two counts must DIFFER, or this
    passes on a parser that hands back everything."""
    _write_usage(usage_store, [_row(ts="2020-01-01T00:00Z"), _row()])
    everything = json.loads(
        run("usage", "--config", usage_store.config, "--json").out)
    recent = json.loads(
        run("usage", "--config", usage_store.config, "--since", "300d",
            "--json").out)
    assert everything["calls"] == 2
    assert recent["calls"] == 1


def test_usage_refuses_a_since_it_cannot_parse(run, keys_file, usage_store):
    """Exit 2, the way argparse does. `60/min` is a rate and not a duration,
    and the house parser already refuses it."""
    result = run("usage", "--config", usage_store.config, "--since", "60/min")
    assert result.code == 2
    assert "60/min" in result.out + result.err


def test_usage_exit_codes(run, keys_file, usage_store):
    """Four cases, and rows 2 and 3 are separated by the KEY file.

    Both produce zero usage rows. Only the key file can say whether the id was
    ever issued, so resolving both paths from the store directory answers them
    on the same branch and a typo in an id reads as a quiet key.
    """
    # 1. no id, nothing recorded.
    result = run("usage", "--config", usage_store.config)
    assert result.code == 0 and "Nothing recorded" in result.out

    run("create", "alice")
    key_id = _only_id(keys_file)

    # 2. an id that IS in the key file, with no rows for it.
    _write_usage(usage_store, [_row(key="k_someoneelse")])
    result = run("usage", key_id, "--config", usage_store.config)
    assert result.code == 0, result.out + result.err
    assert "no recorded calls" in result.out

    # 3. an id that is NOT in the key file, against a key file that EXISTS and
    #    holds a different id, so the exit 1 proves the file was read.
    result = run("usage", "k_notreal", "--config", usage_store.config)
    assert result.code == 1
    assert "k_notreal" in result.out + result.err

    # 4. an unreadable usage file: what could be read, plus a line saying so.
    usage_store.path.chmod(0o000)
    try:
        result = run("usage", "--config", usage_store.config)
    finally:
        usage_store.path.chmod(0o600)
    if os.geteuid() != 0:
        assert result.code == 0
        assert "could not be read" in result.out + result.err


def test_usage_says_how_many_lines_it_could_not_read(run, keys_file, usage_store):
    """A short total reported as the whole record.

    A row from a newer contextlake carrying a thirteenth outcome is DROPPED
    rather than mis-filed as a successful call, so the drop has to be said out
    loud. Both halves: the number is right, and a clean file says nothing.
    """
    usage_store.path.write_text(
        json.dumps(_row()) + "\n"
        + json.dumps(_row(outcome="teleported")) + "\n"
        + '{"ts": "2026-09-07T10:0\n')
    result = run("usage", "--config", usage_store.config)
    assert result.code == 0
    assert "2 line(s)" in result.out
    doc = json.loads(run("usage", "--config", usage_store.config, "--json").out)
    assert doc["unread_lines"] == 2 and doc["calls"] == 1

    _write_usage(usage_store, [_row()])
    assert "line(s)" not in run("usage", "--config", usage_store.config).out
    assert json.loads(
        run("usage", "--config", usage_store.config, "--json").out
    )["unread_lines"] == 0


def test_usage_output_holds_no_key_material(run, keys_file, usage_store):
    """A verb that reads a file about credentials must not print one."""
    created = run("create", "alice")
    _assert_capture_is_live(created.err, "ctxlake_", "create's stderr")
    _write_usage(usage_store, [_row(key=_only_id(keys_file))])
    for argv in (["usage"], ["usage", "--json"]):
        result = run(*argv, "--config", usage_store.config)
        assert not KEY_RE.search(result.out + result.err)


def test_usage_opens_no_store_database(run, keys_file, usage_store, monkeypatch):
    """The verb reads the store DIRECTORY, never the database in it.

    `_open_store` is patched to raise, so a build that reaches it fails rather
    than working on this developer's machine and failing on one with no index.
    """
    from contextlake.kb.cmds import _common

    monkeypatch.setattr(_common, "_open_store",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("opened the store")))
    _write_usage(usage_store, [_row()])
    assert run("usage", "--config", usage_store.config).code == 0
    assert run("list", "--config", usage_store.config).code == 0


def test_keys_list_last_used_has_three_states(run, keys_file, usage_store):
    """The overloaded null, on the surface an operator actually reads.

    `-` with no file, `never` with a file and no row for that key, and a date
    with a row. One value for all three has an operator revoke a key that was
    used seconds ago.
    """
    run("create", "alice")
    run("create", "bob")
    ids = sorted(json.loads(keys_file.read_text())["keys"], key=lambda r: r["name"])
    alice, bob = ids[0]["id"], ids[1]["id"]

    def _cells(result):
        # Keyed by ID, not by name: `log()` prefixes every line with a
        # timestamp, so a positional split lands on the clock and BOTH rows
        # collapse onto one dict entry. That reads as a pass.
        header = next(line for line in result.out.splitlines()
                      if "LAST USED" in line)
        at = header.index("LAST USED")
        rows = {}
        for line in result.out.splitlines():
            for key_id in (alice, bob):
                if key_id in line:
                    rows[key_id] = line[at:].strip()
        return rows

    absent = _cells(run("list", "--config", usage_store.config))
    assert absent == {alice: "-", bob: "-"}

    _write_usage(usage_store, [_row(key=alice, ts="2026-09-07T10:00Z")])
    cells = _cells(run("list", "--config", usage_store.config))
    assert cells[alice] == "2026-09-07"
    assert cells[bob] == "never"

    document = json.loads(run("list", "--config", usage_store.config,
                              "--json").out)
    states = {k["name"]: (k["last_used_state"], k["last_used_at"])
              for k in document["keys"]}
    assert states["alice"] == ("measured", "2026-09-07T10:00Z")
    assert states["bob"] == ("no-rows", None)


def test_keys_list_says_what_never_does_not_mean(run, keys_file, usage_store):
    """The failure the note prevents: revoking a key that is in daily use.

    The usage file is capped, so a key quiet longer than the retained window
    reads `never` too, and `never` on its own says nobody has ever used it.
    """
    run("create", "alice")
    _write_usage(usage_store, [_row(key="k_other")])
    out = run("list", "--config", usage_store.config).out
    assert "retained window" in out


def test_keys_verb_count_is_eight(run):
    """Read off the built parser, never a literal. A ninth verb that parses and
    dispatches nowhere is what the pin exists to catch."""
    assert len(_verbs()) == 8
    assert "usage" in _verbs()
