"""The Windows Task Scheduler adapter.

NOT RUNNABLE HERE. This machine is Linux, so nothing below executes schtasks.
Every test asserts either the rendered command or the exact argv the adapter
would run. That is a real contract and a weaker one than execution, and saying
so is the point.
"""
from __future__ import annotations

import subprocess

import pytest

from contextlake.schedule import jobs as jobstore
from contextlake.schedule.platform import base, windows


def _job(name="default"):
    return jobstore.new_job(name, ["bootstrap"], "auto", "windows")


def _argv():
    return [r"C:\Program Files\venv\python.exe", "-m", "contextlake", "bootstrap"]


def _ok(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


# ---- interval rounding ---------------------------------------------------

def test_the_interval_is_whole_minutes_and_rounds_down():
    """/MO counts whole minutes. Rounds DOWN above a minute, matching cron:
    two backends rounding differently would give one request two intervals."""
    assert windows.nearest_expressible(4200) == (4200.0, 70)
    assert windows.nearest_expressible(4259) == (4200.0, 70)
    assert windows.nearest_expressible(3600) == (3600.0, 60)


def test_a_sub_minute_interval_rounds_up_to_the_floor_never_to_zero():
    """/MO 0 is not a schedule. Below the floor there is nothing smaller to
    round down to, so it rounds up, which is what cron does for the same
    reason."""
    assert windows.nearest_expressible(30) == (60.0, 1)
    assert windows.nearest_expressible(0) == (60.0, 1)


def test_render_says_when_it_had_to_round():
    rendered = windows.WindowsAdapter().render(_job(), 4259, _argv())
    joined = " ".join(rendered["notes"])
    assert "whole minutes" in joined
    assert rendered["interval_s"] == 4200.0

    # ...and stays quiet when nothing was rounded, so the note means something.
    exact = windows.WindowsAdapter().render(_job(), 4200, _argv())
    assert not any("whole minutes" in n for n in exact["notes"])


# ---- the no-catch-up fact ------------------------------------------------

def test_the_adapter_reports_that_it_cannot_replay_a_missed_run():
    """schtasks cannot set StartWhenAvailable. A run missed while the machine
    was off is lost, and status must say so rather than implying systemd's
    behaviour."""
    adapter = windows.WindowsAdapter()
    assert adapter.catches_up_after_sleep is False

    rendered = adapter.render(_job(), 3600, _argv())
    assert any(base.NO_CATCH_UP_PHRASE in n for n in rendered["notes"])


def test_state_carries_the_same_phrase_so_status_does_not_print_it_twice(monkeypatch):
    """The shared phrase is what lets `status` say it once. A different
    wording here would print the identical fact twice, which is the defect
    the cron adapter already had and fixed."""
    monkeypatch.setattr(windows, "_schtasks",
                        lambda *a: _ok(stdout="TaskName: \\contextlake\\default\n"))
    state = windows.WindowsAdapter().state(_job())
    assert any(base.NO_CATCH_UP_PHRASE in n for n in state["notes"])


# ---- quoting -------------------------------------------------------------

def test_the_command_is_quoted_by_windows_rules_not_posix_rules():
    """/TR takes ONE command string parsed by Windows rules. shlex.quote
    produces POSIX quoting, which Windows does not read the same way, and a
    venv path with a space is the ordinary case."""
    # Asserted on the /TR VALUE, the single argv element schtasks receives,
    # not on the rendered display line. The display line quotes that value
    # again for a shell, so its inner quotes are escaped and matching against
    # it would be checking the wrong string.
    calls = []
    import subprocess as _sp

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(windows, "_schtasks",
                            lambda *a: calls.append(a) or _ok())
        windows.WindowsAdapter().install(_job(), 3600, _argv())
    finally:
        monkeypatch.undo()

    argv = list(calls[0])
    tr_value = argv[argv.index("/TR") + 1]

    assert tr_value.startswith(r'"C:\Program Files\venv\python.exe"'), tr_value
    # POSIX single-quoting would be wrong here and is what shlex.quote emits.
    assert "'C:" not in tr_value
    # And it round-trips: Windows parses it back to the argv we started from.
    assert _sp.list2cmdline(_argv()) == tr_value


# ---- install and uninstall: assert the argv ------------------------------

def test_install_creates_the_task_with_the_minute_schedule(monkeypatch):
    calls = []
    monkeypatch.setattr(windows, "_schtasks", lambda *a: calls.append(a) or _ok())

    written = windows.WindowsAdapter().install(_job(), 4200, _argv())

    assert written == [r"\contextlake\default"]
    argv = list(calls[0])
    assert argv[0] == "/Create"
    assert argv[argv.index("/SC") + 1] == "MINUTE"
    assert argv[argv.index("/MO") + 1] == "70"
    assert argv[argv.index("/TN") + 1] == r"\contextlake\default"
    # /F replaces an existing task. Without it a re-install fails, and a wrong
    # interval could never be corrected.
    assert "/F" in argv


def test_install_raises_when_schtasks_refuses(monkeypatch):
    """A failed create must not read as a working schedule. cmd_install
    degrades on OSError by printing the command to run by hand."""
    monkeypatch.setattr(windows, "_schtasks",
                        lambda *a: _ok(returncode=1, stderr="Access is denied."))
    with pytest.raises(OSError, match="Access is denied"):
        windows.WindowsAdapter().install(_job(), 3600, _argv())


def _schtasks_stub(monkeypatch, delete, listing):
    """Answer /Delete with ``delete`` and the unscoped /Query with ``listing``, and return the
    calls made."""
    calls = []

    def _fake(*argv):
        calls.append(argv)
        return delete if argv[0] == "/Delete" else listing

    monkeypatch.setattr(windows, "_schtasks", _fake)
    return calls


# The text of a failed schtasks call is localised, so these stubs use a German one on
# purpose: the adapter must decide from the listing, not from the words.
_DENIED = _ok(returncode=1, stderr="FEHLER: Zugriff verweigert.")
_NOT_FOUND = _ok(returncode=1, stderr="FEHLER: Das System kann die angegebene Datei "
                                      "nicht finden.")


def test_uninstall_deletes_the_task(monkeypatch):
    calls = _schtasks_stub(monkeypatch, _ok(), _ok())
    assert windows.WindowsAdapter().uninstall(_job()) == [r"\contextlake\default"]
    assert [c[0] for c in calls] == ["/Delete"]


def test_uninstall_tolerates_a_task_that_is_not_in_the_listing(monkeypatch):
    """The delete failed and the task is in no listing, so it was already gone. The listing
    names no /TN: a missing \\contextlake folder is then an empty answer, not a failed query."""
    calls = _schtasks_stub(monkeypatch, _NOT_FOUND, _ok(stdout=(
        '"\\contextlake\\nightly","N/A","Bereit"\n'
        '"\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","N/A","Bereit"\n')))

    assert windows.WindowsAdapter().uninstall(_job()) == []
    query = list(calls[1])
    assert query[0] == "/Query" and "/TN" not in query
    assert query[query.index("/FO") + 1] == "CSV"


def test_uninstall_raises_when_the_delete_failed_and_the_task_is_still_listed(monkeypatch):
    """A denied delete leaves the task firing. Read as "already gone", `schedule uninstall`
    dropped the job record and printed success."""
    _schtasks_stub(monkeypatch, _DENIED,
                   _ok(stdout='"\\contextlake\\default","N/A","Bereit"\n'))
    with pytest.raises(OSError, match=r"schtasks /Delete \\contextlake\\default failed"):
        windows.WindowsAdapter().uninstall(_job())


def test_uninstall_compares_the_task_name_without_regard_to_case(monkeypatch):
    """Task names are case-insensitive. A listing that spells the name differently still
    holds the task."""
    _schtasks_stub(monkeypatch, _DENIED,
                   _ok(stdout='"\\contextlake\\Default","N/A","Bereit"\n'))
    with pytest.raises(OSError, match="schtasks /Delete"):
        windows.WindowsAdapter().uninstall(_job())


def test_uninstall_raises_when_the_listing_fails_too(monkeypatch):
    """A listing that failed cannot say the task is gone."""
    _schtasks_stub(monkeypatch, _DENIED, _ok(returncode=1, stderr="FEHLER: Zugriff verweigert."))
    with pytest.raises(OSError, match="Zugriff verweigert"):
        windows.WindowsAdapter().uninstall(_job())


# ---- enumeration ---------------------------------------------------------

def test_installed_names_parses_csv_not_the_localised_table(monkeypatch):
    """/FO CSV /NH because the human-readable table's headers are localised:
    parsing them would work on an English Windows and fail on the user's."""
    seen = {}

    def _query(*argv):
        seen["argv"] = argv
        return _ok(stdout=(
            '"\\contextlake\\default","01/01/2026 00:00:00","Ready"\n'
            '"\\contextlake\\nightly","01/01/2026 00:00:00","Ready"\n'))

    monkeypatch.setattr(windows, "_schtasks", _query)

    assert windows.WindowsAdapter().installed_names() == ["default", "nightly"]
    assert "/FO" in seen["argv"] and "CSV" in seen["argv"]
    assert "/NH" in seen["argv"]


def test_installed_names_returns_none_when_schtasks_cannot_run(monkeypatch):
    """None is "cannot tell". cmd_list reports that as an unchecked platform
    rather than as a clean result, which is the whole point of the pair."""
    def _boom(*a):
        raise OSError("schtasks missing")

    monkeypatch.setattr(windows, "_schtasks", _boom)
    assert windows.WindowsAdapter().installed_names() is None


def test_installed_names_reads_a_missing_folder_as_nothing_installed(monkeypatch):
    monkeypatch.setattr(windows, "_schtasks", lambda *a: _ok(returncode=1))
    assert windows.WindowsAdapter().installed_names() == []


# ---- state ---------------------------------------------------------------

def test_state_reports_not_installed_without_inventing_the_rest(monkeypatch):
    monkeypatch.setattr(windows, "_schtasks", lambda *a: _ok(returncode=1))
    state = windows.WindowsAdapter().state(_job())
    assert state["installed"] is False
    assert state["next_run"] is None
    assert state["exec_path"] is None


def test_state_reads_the_next_run_and_the_command(monkeypatch):
    monkeypatch.setattr(windows, "_schtasks", lambda *a: _ok(stdout=(
        "TaskName:      \\contextlake\\default\n"
        "Next Run Time: 31/08/2026 00:00:00\n"
        "Task To Run:   C:\\venv\\python.exe -m contextlake bootstrap\n")))

    state = windows.WindowsAdapter().state(_job())

    assert state["installed"] is True
    assert state["next_run"] == "31/08/2026 00:00:00"
    assert state["exec_path"] == "C:\\venv\\python.exe"
    # The repeat interval is deliberately None: schtasks does not report it in
    # a form that survives localisation, and "cannot tell" beats a parse that
    # works on one Windows and not another.
    assert state["interval_s"] is None


def test_state_reads_a_quoted_command_whose_path_holds_a_space(monkeypatch):
    """`render` writes the command with list2cmdline, which wraps a path that holds a space in
    quotes, and a venv under C:\\Program Files is the ordinary case. Cutting the value at the
    first space returned the opening quote and half the path, so `status` reported a working
    interpreter as moved."""
    command = subprocess.list2cmdline(_argv())
    # The fixture must differ from the unquoted case, or this test passes without the fix.
    assert command.startswith('"C:\\Program Files\\venv\\python.exe" -m ')
    monkeypatch.setattr(windows, "_schtasks", lambda *a: _ok(stdout=(
        "TaskName:      \\contextlake\\default\n"
        f"Task To Run:   {command}\n")))

    assert windows.WindowsAdapter().state(_job())["exec_path"] == _argv()[0]


@pytest.mark.parametrize("command, program", [
    (r'"C:\Program Files\venv\python.exe" -m contextlake', r"C:\Program Files\venv\python.exe"),
    (r'"C:\Program Files\venv\python.exe"', r"C:\Program Files\venv\python.exe"),
    (r"C:\venv\python.exe -m contextlake", r"C:\venv\python.exe"),
    (r"C:\venv\python.exe", r"C:\venv\python.exe"),
    ('""', None),
    ("", None),
])
def test_program_is_the_quoted_text_or_the_first_word(command, program):
    assert windows._program(command) == program


def test_usable_is_false_on_this_machine():
    assert (windows.WindowsAdapter().usable() is False) or __import__("sys").platform == "win32"


# ---- the working directory a job keeps -----------------------------------
#
# schtasks /Create has no working-directory option, so a job with a directory runs
# `cmd /d /s /c "cd /d "DIR" && COMMAND"`. These checks parse the command the way Windows
# does (the C runtime rules for the schtasks argument, then cmd's /s quote stripping), which
# is the closest this machine gets to running it. They do not replace a run on Windows.

def _cwd_job(cwd):
    return jobstore.new_job("default", ["bootstrap"], "auto", "windows", origin={"cwd": cwd})


def _argv_with_config():
    return [r"C:\Program Files\venv\python.exe", "-m", "contextlake",
            "--config", r"C:\Users\Me\My Work\x.ini", "schedule", "run", "--job", "default"]


def _tr_value(rendered):
    words = windows._split_cmdline(rendered["schtasks-command"])
    return words[words.index("/TR") + 1]


def _cmd_strips(arguments):
    """What `cmd /s /c` runs: the text after /c with its first and last quote removed."""
    assert arguments.startswith('cmd /d /s /c "') and arguments.endswith('"')
    return arguments[len('cmd /d /s /c "'):-1]


def test_a_job_without_a_directory_is_run_directly_as_before():
    rendered = windows.WindowsAdapter().render(_job(), 3600, _argv())
    assert _tr_value(rendered) == subprocess.list2cmdline(_argv())


def test_a_job_with_a_directory_changes_to_it_then_runs_the_command():
    rendered = windows.WindowsAdapter().render(
        _cwd_job(r"C:\Users\Me\My Work"), 3600, _argv_with_config())
    inner = _cmd_strips(_tr_value(rendered))
    assert inner == (r'cd /d "C:\Users\Me\My Work" && "C:\Program Files\venv\python.exe" "-m" '
                     r'"contextlake" "--config" "C:\Users\Me\My Work\x.ini" "schedule" "run" '
                     r'"--job" "default"')


def test_the_command_survives_the_schtasks_argument_and_comes_back_out_of_it():
    cwd = "C:\\Users\\Me\\My Work\\"     # a trailing backslash is the awkward case
    rendered = windows.WindowsAdapter().render(_cwd_job(cwd), 3600, _argv_with_config())
    tr = _tr_value(rendered)
    assert windows._launch(tr) == {"argv": _argv_with_config(), "cwd": cwd}


def test_a_quote_or_control_character_in_the_directory_is_refused():
    for bad in ('C:\\a"b', "C:\\a\nb"):
        with pytest.raises(ValueError):
            windows.WindowsAdapter().render(_cwd_job(bad), 3600, _argv())


# `cmd` reads the whole /TR text before any program does. These paths hold the characters it
# treats as syntax. In the wrapped form each word is in double quotes, which `cmd` reads as text.
WORD_SETS = [
    pytest.param([r"C:\Python\python.exe", "-m", "contextlake", "--config",
                  r"C:\my&dir\cfg.ini", "schedule", "run"], id="ampersand"),
    pytest.param([r"C:\Python\python.exe", "--config", r"C:\a^b\cfg.ini"], id="caret"),
    pytest.param([r"C:\Python\python.exe", "--config", r"C:\a|b\cfg.ini"], id="pipe"),
    pytest.param([r"C:\Python\python.exe", "--config", r"C:\a<b>c\cfg.ini"], id="angles"),
    pytest.param([r"C:\Python\python.exe", "--config", r"C:\proj & co (1)\cfg.ini"],
                 id="space-ampersand-parens"),
    pytest.param([r"C:\Python\python.exe", "--config", "C:\\my dir\\"], id="trailing-backslash"),
    pytest.param([r"C:\Python\python.exe", "--config", "C:\\a&b\\\\"], id="trailing-two"),
    pytest.param([r"C:\Python\python.exe", "", "x"], id="empty-word"),
]


def _outside_quotes(text):
    """The text `cmd` sees outside double quotes. `cmd` toggles on every quote and has no
    escape, so a backslash before a quote does not stop the toggle."""
    quoted, kept = False, []
    for c in text:
        if c == '"':
            quoted = not quoted
        elif not quoted:
            kept.append(c)
    return "".join(kept)


@pytest.mark.parametrize("words", WORD_SETS)
def test_no_cmd_syntax_reaches_cmd_outside_quotes(words):
    rendered = windows.WindowsAdapter().render(_cwd_job(r"C:\proj & co"), 3600, words)
    outside = " ".join(_outside_quotes(_cmd_strips(_tr_value(rendered))).split())
    assert outside == "cd /d &&"        # the one `&&` the wrapper writes, nothing from a path


@pytest.mark.parametrize("words", WORD_SETS)
def test_the_quoted_words_come_back_out_of_the_read_back(words):
    cwd = "C:\\proj & co\\"
    tr = _tr_value(windows.WindowsAdapter().render(_cwd_job(cwd), 3600, words))
    assert windows._launch(tr) == {"argv": words, "cwd": cwd}


@pytest.mark.parametrize("word, quoted", [
    ("abc", '"abc"'), ("", '""'), ("a b", '"a b"'), ("a\\b", '"a\\b"'),
    ("a\\", '"a\\\\"'), ("a\\\\", '"a\\\\\\\\"'),
])
def test_a_word_is_quoted_and_its_trailing_backslashes_are_doubled(word, quoted):
    assert windows._quote_word(word) == quoted
    assert windows._split_cmdline(quoted) == [word]


def test_a_percent_sign_is_refused_in_the_directory_and_in_any_word():
    """`cmd` expands `%NAME%` even inside double quotes."""
    with pytest.raises(ValueError, match="percent sign"):
        windows.WindowsAdapter().render(_cwd_job(r"C:\50%TEMP%\proj"), 3600, _argv())
    for words in ([r"C:\Python\python.exe", "--config", r"C:\50%TEMP%\x.ini"],
                  [r"C:\Py%PATH%\python.exe", "-m", "contextlake"]):
        with pytest.raises(ValueError, match="percent sign"):
            windows.WindowsAdapter().render(_cwd_job(r"C:\proj"), 3600, words)
    with pytest.raises(ValueError, match="percent sign"):
        windows.WindowsAdapter().install(_cwd_job(r"C:\proj"), 3600,
                                         [r"C:\Python\python.exe", "50%"])


def test_an_exclamation_mark_is_refused_in_the_directory_and_in_any_word():
    """`cmd` expands `!NAME!` even inside double quotes when delayed expansion is on, a
    registry setting that nothing in the command turns off. The refusal has the shape the
    percent sign has: a ValueError that names the character and says what to do."""
    with pytest.raises(ValueError, match="exclamation mark") as caught:
        windows.WindowsAdapter().render(_cwd_job(r"C:\Hello!\proj"), 3600, _argv())
    assert "Install from a directory and a config path without them" in str(caught.value)
    for words in ([r"C:\Python\python.exe", "--config", r"C:\!PATH!\x.ini"],
                  [r"C:\Py!thon\python.exe", "-m", "contextlake"],
                  [r"C:\Python\python.exe", "wow!"]):
        with pytest.raises(ValueError, match="exclamation mark"):
            windows.WindowsAdapter().render(_cwd_job(r"C:\proj"), 3600, words)
    with pytest.raises(ValueError, match="exclamation mark"):
        windows.WindowsAdapter().install(_cwd_job(r"C:\proj"), 3600,
                                         [r"C:\Python\python.exe", "50!"])


def test_the_refusal_names_all_three_characters_and_the_control_character():
    with pytest.raises(ValueError) as caught:
        windows.WindowsAdapter().render(_cwd_job("C:\\a!b"), 3600, _argv())
    for named in ("quote", "percent sign", "exclamation mark", "control character"):
        assert named in str(caught.value)


def test_a_quote_or_control_character_in_a_word_is_refused():
    for bad in ('C:\\a"b.ini', "C:\\a\nb.ini"):
        with pytest.raises(ValueError):
            windows.WindowsAdapter().render(
                _cwd_job(r"C:\proj"), 3600, [r"C:\Python\python.exe", "--config", bad])


def test_a_job_with_no_directory_is_not_wrapped_and_its_words_are_not_refused():
    """The wrapper, its quoting and its refusals are for the `cmd` form only, and the direct
    form is unchanged. Whether Task Scheduler expands `%VAR%` in the direct form is
    [UNVERIFIED] without a Windows host, and outside this change."""
    words = [r"C:\Python\python.exe", "--config", r"C:\50%TEMP%\a&b\x!y!.ini"]
    rendered = windows.WindowsAdapter().render(_job(), 3600, words)
    assert _tr_value(rendered) == subprocess.list2cmdline(words)
    assert not _tr_value(rendered).startswith("cmd ")


def test_install_creates_the_task_with_the_same_command_render_shows(monkeypatch):
    calls = []
    monkeypatch.setattr(windows, "_schtasks", lambda *a: calls.append(a) or _ok())
    job = _cwd_job(r"C:\Users\Me\My Work")
    windows.WindowsAdapter().install(job, 3600, _argv_with_config())
    argv = list(calls[0])
    rendered = windows.WindowsAdapter().render(job, 3600, _argv_with_config())
    assert argv[argv.index("/TR") + 1] == _tr_value(rendered)
    assert argv[argv.index("/TR") + 1].startswith("cmd /d /s /c ")


def test_state_finds_the_interpreter_inside_the_cmd_wrapper(monkeypatch):
    """Read as a plain command, the program is `cmd`, which is on no venv path, and `status`
    would call the interpreter moved."""
    command = _tr_value(windows.WindowsAdapter().render(
        _cwd_job(r"C:\Users\Me\My Work"), 3600, _argv_with_config()))
    monkeypatch.setattr(windows, "_schtasks", lambda *a: _ok(stdout=(
        "TaskName:      \\contextlake\\default\n"
        f"Task To Run:   {command}\n")))
    state = windows.WindowsAdapter().state(_job())
    assert state["exec_path"] == _argv_with_config()[0]
    assert state["launch"] == {"argv": _argv_with_config(), "cwd": r"C:\Users\Me\My Work"}


def test_state_reads_a_task_earlier_versions_wrote_as_having_no_directory(monkeypatch):
    command = subprocess.list2cmdline(_argv())
    monkeypatch.setattr(windows, "_schtasks", lambda *a: _ok(stdout=(
        f"TaskName:      \\contextlake\\default\nTask To Run:   {command}\n")))
    assert windows.WindowsAdapter().state(_job())["launch"] == {"argv": _argv(), "cwd": None}


@pytest.mark.parametrize("text, words", [
    (r'"C:\a b\p.exe" -m x', [r"C:\a b\p.exe", "-m", "x"]),
    (r'p.exe "a b\\" c', ["p.exe", "a b\\", "c"]),
    (r'p.exe a\"b', ['p.exe', 'a"b']),
    ("  p.exe   x  ", ["p.exe", "x"]),
    ("", []),
])
def test_the_command_line_splitter_follows_the_c_runtime_rules(text, words):
    assert windows._split_cmdline(text) == words


@pytest.mark.parametrize("words", [
    [r"C:\a b\p.exe", "-m", "x"], ["p", 'say "hi"', "c d\\"], ["p", "", "x"],
])
def test_list2cmdline_output_splits_back_into_the_same_words(words):
    assert windows._split_cmdline(subprocess.list2cmdline(words)) == words
