"""The cron adapter must never lose a line of the user's crontab.

Every test here drives the real ``install`` and ``uninstall`` code against a
FAKE ``crontab`` binary. ``PATH`` holds only the fake's directory, so the real
binary cannot be reached, and each test asserts the fake was called, so a run
that silently went somewhere else fails. Stubbing ``_read_crontab`` instead is
what hid D01: the stub never fails the way the binary does.

D01: a failed ``crontab -l`` was read as an empty crontab, and the install
then wrote the contextlake block over every user job.
D09: a BEGIN marker with no END swallowed every line after it, the block's
``MAILTO=""`` silenced mail for every user job after it, and bytes that are
not UTF-8 were replaced on the way through.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

from contextlake.schedule import jobs
from contextlake.schedule.platform import cron

_FAKE = '''#!{python}
import os
import sys

d = os.environ.get("FAKE_CRONTAB_DIR")
if not d:
    sys.stderr.write("fake crontab: FAKE_CRONTAB_DIR is not set\\n")
    sys.exit(3)


def p(name):
    return os.path.join(d, name)


with open(p("calls"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args == ["-l"]:
    if os.path.exists(p("l_rc")):
        with open(p("l_stderr"), "rb") as f:
            sys.stderr.buffer.write(f.read())
        with open(p("l_rc")) as f:
            sys.exit(int(f.read()))
    with open(p("current"), "rb") as f:
        sys.stdout.buffer.write(f.read())
    sys.exit(0)
if args == ["-"]:
    data = sys.stdin.buffer.read()
    with open(p("current"), "wb") as f:
        f.write(data)
    for name in ("l_rc", "l_stderr"):
        if os.path.exists(p(name)):
            os.remove(p(name))
    sys.exit(0)
sys.stderr.write("fake crontab: unexpected arguments %r\\n" % (args,))
sys.exit(2)
'''


class FakeCrontab:
    """A ``crontab`` binary whose stored crontab is a file under tmp_path."""

    def __init__(self, root):
        self.state = root / "fake-crontab-state"
        self.state.mkdir()
        self.bin = root / "fake-crontab-bin"
        self.bin.mkdir()
        script = self.bin / "crontab"
        script.write_text(_FAKE.format(python=sys.executable))
        script.chmod(0o755)

    def seed(self, data: bytes):
        (self.state / "current").write_bytes(data)

    def fail_list(self, stderr: bytes, rc: int = 1):
        """Make ``crontab -l`` print ``stderr`` and exit ``rc``."""
        (self.state / "l_stderr").write_bytes(stderr)
        (self.state / "l_rc").write_text(str(rc))

    def stored(self) -> bytes:
        return (self.state / "current").read_bytes()

    def calls(self) -> list:
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeCrontab(tmp_path)
    monkeypatch.setenv("PATH", str(f.bin))
    monkeypatch.setenv("FAKE_CRONTAB_DIR", str(f.state))
    return f


@pytest.fixture
def logged(monkeypatch):
    """Every message ``cron`` logs. Recorded at the call, not read back
    through capsys, which the package logger does not reliably reach."""
    lines = []
    monkeypatch.setattr(cron, "log", lambda message, *a, **k: lines.append(str(message)))
    return lines


def _job(name="default"):
    return jobs.new_job(name, ["version"], "auto", "cron")


ARGV = ["/opt/py", "-m", "contextlake", "schedule", "run", "--job", "default"]
BEGIN = cron.BEGIN.format(name="default").encode()
END = cron.END.format(name="default").encode()
USER = b"0 2 * * * /home/u/backup.sh\n30 4 * * 1 /home/u/weekly.sh\n"


def _install(name="default"):
    return cron.CronAdapter().install(_job(name), 3600.0, ARGV)


def _backups(home_cache):
    directory = home_cache / "contextlake" / "crontab-backups"
    return sorted(directory.iterdir()) if directory.exists() else []


@pytest.fixture
def cache(tmp_path):
    """The cache root the isolated HOME resolves to (conftest moves HOME)."""
    return tmp_path / "isolated-home" / ".cache"


def _outside_block(data: bytes, name=b"default") -> bytes:
    """``data`` with the marked block for ``name`` cut out. Written here
    on bytes, apart from the code under test, so the two cannot share a bug."""
    begin = b"# >>> contextlake (" + name + b") >>>"
    end = b"# <<< contextlake (" + name + b") <<<"
    out, inside = [], False
    for line in data.split(b"\n"):
        if line.strip() == begin:
            inside = True
            continue
        if inside:
            if line.strip() == end:
                inside = False
            continue
        out.append(line)
    return b"\n".join(out)


def _mailto_for(data: bytes, marker: bytes):
    """The MAILTO cron applies to the first line containing ``marker``:
    the last ``MAILTO=`` line above it, or None for cron's default."""
    current = None
    for line in data.split(b"\n"):
        stripped = line.strip()
        if stripped.startswith(b"MAILTO") and b"=" in stripped:
            key, _, value = stripped.partition(b"=")
            if key.strip() == b"MAILTO":
                current = value.strip()
        if marker in line:
            return current
    raise AssertionError(f"{marker!r} not in the crontab")


# ---- D01: a failed read must never become a write ------------------------

def test_a_failed_read_refuses_to_write_and_names_the_error(fake, cache):
    fake.seed(USER)
    fake.fail_list(b"/var/spool/cron/fakeuser: Permission denied\n")

    with pytest.raises(OSError, match="Permission denied"):
        _install()

    assert fake.calls() == ["-l"]          # never reached `crontab -`
    assert fake.stored() == USER           # every user job is still there
    assert _backups(cache) == []


def test_a_failed_read_refuses_the_uninstall_too(fake):
    fake.seed(USER + BEGIN + b"\n0 * * * * /x\n" + END + b"\n")
    fake.fail_list(b"/var/spool/cron/fakeuser: Permission denied\n")

    with pytest.raises(OSError, match="Permission denied"):
        cron.CronAdapter().uninstall(_job())

    assert fake.calls() == ["-l"]


@pytest.mark.parametrize("stderr", [
    b"no crontab for fakeuser\n",           # cronie, Debian cron
    b"crontab: no crontab for fakeuser\n",  # FreeBSD and macOS, through errx(3)
])
def test_no_crontab_yet_is_a_first_install_not_a_failure(fake, cache, stderr):
    fake.fail_list(stderr)

    assert _install() == ["crontab"]

    assert fake.calls() == ["-l", "-"]
    stored = fake.stored()
    assert stored.startswith(BEGIN + b"\n")
    assert stored.endswith(END + b"\n")
    # Nothing existed, so there was nothing to copy.
    assert _backups(cache) == []


@pytest.mark.parametrize("rc,stderr", [
    (1, b"no crontab for fakeuser\nand then something else\n"),
    (2, b"no crontab for fakeuser\n"),
    (1, b"crontab: can't open 'fakeuser': No such file or directory\n"),
    (1, b"cannot connect: no crontab for fakeuser\n"),
    (1, b""),
])
def test_only_the_exact_no_crontab_message_counts_as_empty(fake, rc, stderr):
    """Anything that is not the one message, with the one exit status, is a
    failure. Reading it as "empty" is what deleted the user's jobs."""
    fake.seed(USER)
    fake.fail_list(stderr, rc)

    with pytest.raises(OSError, match="crontab -l failed"):
        _install()

    assert fake.calls() == ["-l"]
    assert fake.stored() == USER


def test_an_existing_crontab_keeps_its_lines_and_is_backed_up_first(fake, cache, logged):
    fake.seed(USER)

    assert _install() == ["crontab"]

    assert fake.calls() == ["-l", "-"]
    stored = fake.stored()
    assert stored.startswith(USER)
    assert BEGIN in stored
    backups = _backups(cache)
    assert len(backups) == 1
    assert backups[0].read_bytes() == USER
    # The copy can hold secrets set in the crontab's environment lines.
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert any(str(backups[0]) in line for line in logged)


def test_uninstall_backs_up_before_it_writes(fake, cache, logged):
    before = USER + BEGIN + b"\n0 * * * * /x\n" + END + b"\n"
    fake.seed(before)

    assert cron.CronAdapter().uninstall(_job()) == ["crontab"]

    assert fake.calls() == ["-l", "-"]
    assert fake.stored() == USER
    backups = _backups(cache)
    assert [b.read_bytes() for b in backups] == [before]
    assert any(str(backups[0]) in line for line in logged)


def test_a_backup_that_cannot_be_saved_stops_the_write(fake, tmp_path, monkeypatch):
    """No copy, no write. The cache root is a regular file here, so the
    backup directory cannot be created under it."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    fake.seed(USER)

    with pytest.raises(OSError):
        _install()

    assert fake.calls() == ["-l"]
    assert fake.stored() == USER


def test_installed_names_says_cannot_tell_when_the_read_fails(fake):
    """``None`` is "could not enumerate". ``[]`` would claim nothing is
    installed, which nobody measured."""
    fake.seed(USER)
    fake.fail_list(b"/var/spool/cron/fakeuser: Permission denied\n")

    assert cron.CronAdapter().installed_names() is None
    assert fake.calls() == ["-l"]


# ---- D09: lines outside the block come back byte for byte ----------------

FIXTURES = {
    "escaped-space-in-PATH": b"PATH=/usr/bin:/opt/my\\ tools/bin\n0 1 * * * run-it --flag\n",
    "non-utf8-comment": b"# caf\xe9 \xff\xfe is not UTF-8\n0 1 * * * /home/u/x.sh\n",
    "carriage-return": b"0 2 * * * /home/u/a.sh\r\n# saved by a Windows editor\r\n",
    "user-mailto-first": b"MAILTO=ops@example.org\n0 2 * * * /home/u/backup.sh\n",
    # A form feed is a line break to str.splitlines() but not to cron.
    "form-feed": (b"# page one\x0c" + BEGIN + b"\n"
                  b"0 4 * * * /home/u/after.sh\n"),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_install_then_uninstall_keeps_every_user_byte(fake, name):
    original = FIXTURES[name]
    fake.seed(original)

    _install()
    installed = fake.stored()
    assert installed.count(BEGIN + b"\n") >= 1
    assert _outside_block(installed) == original

    cron.CronAdapter().uninstall(_job())
    assert fake.stored() == original
    assert fake.calls() == ["-l", "-", "-l", "-"]


def test_a_crontab_with_no_final_newline_gains_only_that_newline(fake):
    """cron ignores a last line with no newline (crontab(5), cronie: "cron
    requires that each entry in a crontab end in a newline character"), so
    appending the block straight onto it would disable the user's last job.
    That one newline is the only byte the user's text gains."""
    original = b"# keep me\n0 5 * * * /home/u/backup.sh"
    fake.seed(original)

    _install()
    assert _outside_block(fake.stored()) == original + b"\n"

    cron.CronAdapter().uninstall(_job())
    assert fake.stored() == original + b"\n"


def test_replacing_a_block_in_the_middle_keeps_both_sides(fake):
    """An existing block, written by an earlier version with its MAILTO line,
    sits between user lines. Reinstalling replaces it in place."""
    old_block = (BEGIN + b"\n"
                 b'MAILTO=""\n'
                 b"0 * * * * /old/py -m contextlake schedule run --job default\n"
                 + END + b"\n")
    before = b"MAILTO=ops@example.org\n0 2 * * * /a\n"
    after = b"45 6 * * * /home/u/later.sh\n# caf\xe9\n"
    fake.seed(before + old_block + after)

    _install()

    stored = fake.stored()
    assert stored.startswith(before)
    assert stored.endswith(after)
    assert stored.count(BEGIN) == 1
    assert b"/old/py" not in stored


# ---- D09: the block must not change MAILTO for the user's lines ----------

def test_the_block_sets_no_mailto(fake):
    fake.seed(USER)
    _install()
    block = fake.stored()[len(USER):]
    assert b"MAILTO" not in block


def test_a_user_job_after_the_block_keeps_the_users_mailto(fake):
    fake.seed(b"MAILTO=ops@example.org\n0 2 * * * /home/u/backup.sh\n")
    _install()
    # The user adds a job later, below the block, with `crontab -e`.
    fake.seed(fake.stored() + b"45 6 * * * /home/u/added-later.sh\n")

    assert _mailto_for(fake.stored(), b"added-later.sh") == b"ops@example.org"


def test_reinstalling_over_an_old_block_gives_later_jobs_their_mail_back(fake):
    """An earlier version wrote MAILTO="" inside the block. Reinstalling must
    remove it, or the user's jobs below stay silenced."""
    fake.seed(BEGIN + b"\n"
              b'MAILTO=""\n'
              b"0 * * * * /old/py -m contextlake schedule run --job default\n"
              + END + b"\n"
              b"45 6 * * * /home/u/below.sh\n")
    assert _mailto_for(fake.stored(), b"below.sh") == b'""'

    _install()

    assert _mailto_for(fake.stored(), b"below.sh") is None


def test_the_job_line_discards_its_own_output_so_cron_sends_no_mail():
    """cron mails a job's output (cron(8)). With the output discarded in the
    command itself there is nothing to mail, and no environment line is
    needed to stop it."""
    line = cron.CronAdapter().render(_job(), 3600.0, ARGV)["crontab"]
    assert line.rstrip("\n").endswith(" >/dev/null 2>&1")
    assert "MAILTO" not in line


def test_state_still_reads_a_block_this_version_wrote(fake):
    fake.seed(USER)
    _install()

    state = cron.CronAdapter().state(_job())

    assert state["installed"] is True
    assert state["exec_path"] == "/opt/py"
    assert state["interval_s"] == 3600


def test_state_reads_the_real_block_not_a_marker_inside_a_user_line(fake):
    """The readers split lines the way `splice` does. Split on a form feed
    too, and the user's next job is read as the block's command, so
    `status` would report the user's script as a missing interpreter."""
    fake.seed(FIXTURES["form-feed"])
    _install()

    state = cron.CronAdapter().state(_job())

    assert state["exec_path"] == "/opt/py"
    assert state["interval_s"] == 3600


# ---- D09: a block with no END is refused, never guessed at ---------------

NO_END = (b"0 2 * * * /home/u/backup.sh\n"
          + BEGIN + b"\n"
          b'MAILTO=""\n'
          b"0 * * * * /opt/py -m contextlake\n"
          b"30 4 * * 1 /home/u/weekly.sh\n"
          b"15 5 * * * /home/u/report.sh\n")


def test_install_refuses_a_block_with_no_end_marker(fake, cache):
    fake.seed(NO_END)

    with pytest.raises(OSError, match="line 2"):
        _install()

    assert fake.calls() == ["-l"]
    assert fake.stored() == NO_END
    assert _backups(cache) == []


def test_uninstall_refuses_a_block_with_no_end_marker(fake):
    fake.seed(NO_END)

    with pytest.raises(OSError, match="no matching"):
        cron.CronAdapter().uninstall(_job())

    assert fake.calls() == ["-l"]
    assert fake.stored() == NO_END


def test_a_second_begin_before_the_end_is_refused():
    """BEGIN, user lines, BEGIN, END: the first block lost its END. Treating
    the second BEGIN as part of one block would delete the lines between."""
    text = (BEGIN + b"\n0 * * * * /x\n"
            b"30 4 * * 1 /home/u/weekly.sh\n"
            + BEGIN + b"\n0 * * * * /x\n" + END + b"\n").decode()
    with pytest.raises(ValueError, match="line 1"):
        cron.splice(text, "default", "0 * * * * /y\n")
    with pytest.raises(ValueError, match="line 1"):
        cron.splice(text, "default", None)


def test_another_jobs_block_with_no_end_does_not_block_this_one():
    """Only the block being edited has to be well formed. Another job's
    markers are user text to this call and pass through untouched."""
    other = "# >>> contextlake (nightly) >>>\n0 3 * * * /n\n"
    text = cron.splice(other, "default", "0 * * * * /x\n")
    assert text.startswith(other)


@pytest.mark.skipif(os.name != "posix", reason="the fake crontab is a POSIX script")
def test_the_fake_is_the_only_crontab_on_path(fake):
    """Guards every test above: PATH holds only the fake."""
    import shutil

    assert shutil.which("crontab") == str(fake.bin / "crontab")
