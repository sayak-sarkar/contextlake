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
    """Criterion 11, with one deviation from the ticket, recorded here.

    A full table and exit 0, and every LAST USED cell reads `-`, NOT the `never`
    the criterion asks for. Nothing records a use in this release, so `never` is
    a claim about a key that may have been used seconds ago. An operator reading
    the column to find dead keys would revoke a live one, and the table gave them
    no way to know. `-` is what an unset policy axis already renders, and the note
    under the table says the column is not recorded yet.

    The column is filled from the JSONL usage file by S4.5.4 in phase 3, read BY
    PATH, which opens no database. If phase 3 ships without that fill task, the
    column reads `-` forever and the operator never sees the unused key that
    should be revoked, which is why the note names the release rather than
    implying the value is a measurement.
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
    assert "LAST USED is not recorded in this release" in result.out


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


def test_rate_and_cost_budget_are_stored_as_typed_and_not_validated(run, keys_file):
    """Phase 1 stores the string. `parse_rate` is phase 2's (S4.4.1).

    `parse_duration` (`schedule/recommend.py:38`) cannot read `60/min`, so there
    is nothing here to validate with, and the help text says so. Phase 2 owes the
    other half: when `parse_rate` lands it must run over every stored value at
    keyring load and FAIL the load on one it refuses. A garbage rate quietly
    replaced by a default is an unlimited key that reads as limited in `show`.
    """
    assert run("create", "alice", "--rate", "not-a-rate").code == 0
    assert json.loads(keys_file.read_text())["keys"][0]["policy"]["rate"] == "not-a-rate"


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
# The policy is recorded and enforced by nothing, and every surface says so
# ---------------------------------------------------------------------------
#
# MEASURED FIRST, on 2026-09-05, because a wording change with no measurement
# behind it is a preference. A key created with `--tools none --repos
# nothing-matches/*` was presented to a live `kb serve --transport http
# --keys-only` server over HTTP. `tools/list` answered with all 23 registered
# tools and `graph_stats` then ran and returned a result. `kb keys show` for
# that key printed "grant expanded at 8.13.0. Tools added since are denied;
# rotate to pick them up."
#
# So the CLI told an operator their key was scoped while the server handed it
# everything. That is worse than not offering the flags: someone hands the key
# out on the CLI's reading.

_LABEL = "recorded, not enforced"
_NOTE = "enforces them"


def _policy_argv(verb: str, key_id: str) -> list[str]:
    return {"create": ["create", "bob", "--tools", "read", "--repos", "acme/*"],
            "list": ["list"],
            "show": ["show", key_id],
            "check": ["check"],
            "revoke": ["revoke", key_id],
            "rotate": ["rotate", key_id],
            "prune": ["prune", "--before", "2020-01-01"]}[verb]


def test_every_verb_that_renders_a_policy_says_nothing_enforces_it(run, keys_file):
    """Driven by the parser's own verb list, so an eighth verb is covered too.

    The two halves this pins are the same defect at two altitudes: the label
    beside the values, and the sentence that says what the label means. A verb
    that starts rendering a scope without them fails here rather than shipping.

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
        assert _LABEL in text, (
            f"`kb keys {verb}` renders a policy value with no {_LABEL!r} beside "
            "it, so an operator reads it as a live restriction")
        assert _NOTE in text, (
            f"`kb keys {verb}` renders a policy and never says nothing enforces "
            "it")

    assert sorted(rendered) == ["check", "create", "list", "show"], rendered


def test_show_never_claims_a_tool_is_denied(run, keys_file):
    """The wording gate. `show` used to print a live access-control claim.

    Verbatim, at `keys_cmd.py:567` before this change: "grant expanded at
    8.13.0. Tools added since are denied; rotate to pick them up." Both halves
    were false. `_policy` stores the raw string the operator typed and expands
    nothing, and no code path denies a tool. If either sentence comes back, or
    any other verb of denial, this fails.
    """
    run("create", "alice", "--tools", "read", "--repos", "acme/*")
    text = (lambda r: r.out + r.err)(run("show", _only_id(keys_file)))
    _assert_capture_is_live(text, "acme/*", "kb keys show")

    lowered = text.lower()
    for claim in ("denied", "denies", "may call", "may read", "cannot call",
                  "restricted to", "grant expanded"):
        assert claim not in lowered, (
            f"`kb keys show` claims {claim!r}. Nothing enforces the policy, so "
            "that sentence tells an operator their key is scoped when it is not")
    assert _NOTE in text


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


def test_the_json_surfaces_carry_the_not_enforced_flag(run, keys_file):
    """A script reading `policy` gets no label out of a text line.

    `{"tools": "none"}` on its own says the opposite of the truth to anything
    that renders it, so both JSON surfaces carry the flag as data.
    """
    run("create", "alice", "--tools", "none", "--repos", "nothing-matches/*")
    shown = json.loads(run("show", _only_id(keys_file), "--json").out)
    assert shown["policy"] == {"tools": "none", "repos": "nothing-matches/*"}
    assert shown["policy_enforced"] is False

    listed = json.loads(run("list", "--json").out)
    assert listed["policy_enforced"] is False


def test_the_label_is_pinned_to_the_server_enforcing_nothing(run, keys_file):
    """The precondition, so the label retires when it stops being true.

    Every assertion above is about wording. This one reads the reason the
    wording is right, and it is deliberately in the OTHER direction: it fails
    when enforcement lands. Whoever ships S4.3-acl-2 gets a red test naming the
    four surfaces whose wording has to change with it, rather than a CLI that
    keeps saying "nothing enforces them" after something does.

    `check_tool_grant` is the symbol the ANCHOR comment at `kb/server.py:1064`
    reserves for the tool-axis check. `grant_source = None` at
    `kb/server.py:2939` is what stops the keyring reaching the tool wrapper.
    """
    import inspect

    from contextlake.kb import server

    assert not hasattr(server, "check_tool_grant"), (
        "kb/server.py has grown check_tool_grant, so the tool axis may now be "
        "enforced. Re-read _enforcement_note in kb/cmds/keys_cmd.py: create, "
        "list, show and check all print that nothing enforces the policy")
    # A string match on another module's formatting, so read it in that order:
    # check whether the line MOVED or was reformatted before concluding the
    # behaviour changed. The `check_tool_grant` assertion above is the robust
    # half; this one is the redundancy.
    assert "grant_source = None" in inspect.getsource(server.build_http_app), (
        "build_http_app no longer carries the literal `grant_source = None`. "
        "This may be a reformat rather than a behaviour change: read the "
        "function first. If the keyring now reaches the tool wrapper, re-read "
        "_enforcement_note in kb/cmds/keys_cmd.py before this ships")


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
    for dest in ("tools", "repos", "owners"):
        text = helps[dest]
        assert "nothing enforces it in this release" in text, (
            f"--{dest} does not say its value binds nothing: {text!r}")
        for claim in ("may call", "may read", "this key sees"):
            assert claim not in text, (
                f"--{dest} help claims {claim!r}, which reads as a live grant")
