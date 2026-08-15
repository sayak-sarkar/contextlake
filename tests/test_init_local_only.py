"""`init` must work for someone with repositories on disk and no forge.

That persona is exactly who README's "Quickstart: one repo, no setup" addresses, and
`init` had no path for them: the group check ran before the knowledge-layer branch, so
BOTH the interactive form (press Enter through it) and the documented
`--skip-interactive` "all defaults" form exited 2 and wrote nothing. `--no-kb` did not
help either. A user following QUICKSTART step 3 stopped there.

The error was also incomplete on its own terms — it said "pass --group or answer the
prompt" to somebody who had just done neither because they have nothing to pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run(args, cwd, home):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1"}
    return subprocess.run([sys.executable, "-m", "contextlake", *args], cwd=str(cwd),
                          env=env, capture_output=True, text=True)


def test_no_mirror_writes_a_knowledge_config_and_no_mirror_ini(tmp_path):
    """THE LOAD-BEARING ASSERTION: it completes, and it writes the right ONE file.

    A mirror INI here would name a group that does not exist — the exact placeholder
    the group check refuses to create, arrived at from the other direction.
    """
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()

    r = _run(["init", "--local", "--no-mirror", "--skip-interactive"], cwd=ws, home=home)
    assert r.returncode == 0, f"the local-only path still fails:\n{(r.stdout + r.stderr)[-1500:]}"
    assert (ws / ".contextlake.kb.toml").is_file(), "no knowledge config was written"
    assert not (ws / ".contextlake.ini").is_file(), (
        "a mirror INI was written for a user who has no forge, so it names a group "
        "that does not exist")


def test_no_mirror_points_at_a_command_that_can_actually_run(tmp_path):
    """`bootstrap` mirrors first and refuses without a group, so ending this user's
    setup by telling them to run it would put them straight back on an exit 2."""
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()

    out = _run(["init", "--local", "--no-mirror", "--skip-interactive"],
               cwd=ws, home=home).stdout
    assert "kb index --source" in out, f"no runnable next step given:\n{out[-800:]}"
    assert "contextlake bootstrap" not in out, (
        "the local-only path recommends `bootstrap`, which refuses without a group")


def test_the_missing_group_error_names_the_way_out(tmp_path):
    """Still refuses without a group — that refusal is correct and deliberate — but it
    now tells a user with no forge what to do instead of leaving them stuck."""
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()

    r = _run(["init", "--local", "--skip-interactive"], cwd=ws, home=home)
    out = r.stdout + r.stderr
    assert r.returncode == 2, "the placeholder refusal must stay"
    assert "--no-mirror" in out, f"the error offers no escape:\n{out[-800:]}"
    assert "kb index --source" in out


def test_a_normal_init_still_writes_both_files(tmp_path):
    """The near-miss. `--no-mirror` must not become the accidental default: a user WITH
    a group still gets the mirror config they asked for."""
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()

    r = _run(["init", "--local", "--platform", "github", "--group", "demo-org",
              "--skip-interactive"], cwd=ws, home=home)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1000:]
    assert (ws / ".contextlake.ini").is_file(), "the mirror config went missing"
    assert (ws / ".contextlake.kb.toml").is_file()
