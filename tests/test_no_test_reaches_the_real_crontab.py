"""Every test runs with a stub `crontab` first on PATH, put there by conftest.

A help example run by `test_every_help_example_runs` probed cron, so each full run read
the operator's real crontab. The stub answers "no crontab" to `-l` and refuses writes.
These tests check the stub is what a test reaches, not only that the fixture exists.

They call the stub by the absolute path they have just checked, never by the name
`crontab`. If the fixture were gone, `crontab -` by name would replace the operator's
real crontab with this test's line. A break-test of the fixture came within one
assertion of doing that.
"""

import shutil
import subprocess


def _stub() -> str:
    found = shutil.which("crontab")
    assert found is not None
    assert "crontab-stub" in found, f"PATH reaches {found}, not the conftest stub"
    return found


def test_crontab_on_path_is_the_stub():
    _stub()


def test_the_stub_reports_no_crontab_and_refuses_writes():
    stub = _stub()
    listed = subprocess.run([stub, "-l"], capture_output=True, text=True)
    assert listed.returncode == 1
    assert listed.stderr.strip() == "no crontab for test"

    wrote = subprocess.run([stub, "-"], input="* * * * * true\n",
                           capture_output=True, text=True)
    assert wrote.returncode == 97
    assert "tried to write the real crontab" in wrote.stderr
