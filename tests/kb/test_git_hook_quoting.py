"""The post-commit hook must pass paths and ids to the command as plain text.

`git_hook._block` put the repo path, the repo id and the config path inside double quotes.
Inside double quotes `$(...)`, backticks and `$VAR` still run, so a repo directory named
``proj-$(touch pwned)`` or a repo id taken from a remote URL such as
``host/$(touch pwned)`` ran that command on every commit. The hook runs with the user's
rights, from git, with nobody watching.

These tests render the hook and run the body with `sh`. `git` and the contextlake command
are stubs on PATH: the stub writes its argv to a file, so the tests check what the command
RECEIVED as well as what the shell did. A hook that never ran the command would pass a bare
"sentinel not created" check, so the argv check comes first.
"""

from __future__ import annotations

import shlex
import stat
import subprocess
import time

import pytest

from contextlake import launcher
from contextlake.kb import git_hook


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A PATH with a recording stub for the command and a stub `git`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv.log"
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()

    stub = bindir / "clstub"
    stub.write_text(
        '#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> ' + shlex.quote(str(argv_log))
        + '; done\nprintf "END\\n" >> ' + shlex.quote(str(argv_log)) + "\n")
    gitstub = bindir / "git"
    gitstub.write_text("#!/bin/sh\necho " + shlex.quote(str(gitdir)) + "\n")
    for f in (stub, gitstub):
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    # `launch_command` quotes the interpreter path itself; the stub stands in for it.
    monkeypatch.setattr(launcher, "launch_command", lambda **k: shlex.quote(str(stub)))

    def run(body: str, cwd=None) -> list[str]:
        """Run the hook body with sh, wait for the detached command, return its argv."""
        hook = tmp_path / "hook.sh"
        hook.write_text("#!/bin/sh\n" + body)
        subprocess.run(["sh", str(hook)], cwd=cwd or tmp_path, check=False,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                       env={"PATH": f"{bindir}:/usr/bin:/bin"})
        deadline = time.monotonic() + 10      # bounded: the hook detaches the command with &
        while time.monotonic() < deadline:
            if argv_log.exists() and argv_log.read_text().endswith("END\n"):
                return argv_log.read_text().splitlines()[:-1]
            time.sleep(0.05)
        pytest.fail("the hook never ran the command (no argv recorded within 10 s)")

    return run


@pytest.fixture(params=["command-substitution", "backticks", "quote-breakout",
                        "single-quote-and-space", "variables"])
def payload(request, tmp_path):
    s = tmp_path / "pwned"
    return {
        "command-substitution": f"$(touch {s})",
        "backticks": f"`touch {s}`",
        "quote-breakout": f'x"; touch {s}; echo "',
        "single-quote-and-space": "it's a name",
        "variables": "$HOME-${PATH}",
    }[request.param]


def test_repo_path_reaches_the_command_as_plain_text(rig, tmp_path, payload):
    path = f"{tmp_path}/proj-{payload}"
    argv = rig(git_hook._block(path, "team/app", None))
    assert argv == ["kb", "index", path, "--repo", "team/app"], argv
    assert not (tmp_path / "pwned").exists(), "the injected command ran"


def test_repo_id_reaches_the_command_as_plain_text(rig, tmp_path, payload):
    rid = f"host/{payload}"
    argv = rig(git_hook._block(f"{tmp_path}/proj", rid, None))
    assert argv == ["kb", "index", f"{tmp_path}/proj", "--repo", rid], argv
    assert not (tmp_path / "pwned").exists(), "the injected command ran"


def test_config_path_reaches_the_command_as_plain_text(rig, tmp_path, payload):
    cfg = f"{tmp_path}/c-{payload}.toml"
    argv = rig(git_hook._block(f"{tmp_path}/proj", "team/app", cfg))
    assert argv == ["--config", cfg, "kb", "index", f"{tmp_path}/proj", "--repo", "team/app"], argv
    assert not (tmp_path / "pwned").exists(), "the injected command ran"


def test_install_into_a_directory_named_like_a_command(rig, tmp_path):
    """End to end through `install`, with a directory name that can exist on disk (it has no
    `/`). The command runs in the hook's working directory, which is where `touch pwned`
    would land."""
    repo = tmp_path / "proj-$(touch pwned)"
    (repo / ".git").mkdir(parents=True)
    assert git_hook.install(str(repo), "team/app") == "installed"
    body = (repo / ".git" / "hooks" / "post-commit").read_text()

    argv = rig(body.split("\n", 1)[1])          # drop the shebang line; `rig` adds its own
    assert argv == ["kb", "index", str(repo.resolve()), "--repo", "team/app"], argv
    assert not (tmp_path / "pwned").exists(), "the injected command ran"


def test_ordinary_values_are_still_passed_through(rig, tmp_path):
    """The near-miss: quoting must not change what a normal repo gets."""
    argv = rig(git_hook._block(f"{tmp_path}/my repo", "gitlab.com/acme/api",
                               f"{tmp_path}/c.toml"))
    assert argv == ["--config", f"{tmp_path}/c.toml", "kb", "index", f"{tmp_path}/my repo",
                    "--repo", "gitlab.com/acme/api"]


def test_the_rendered_block_quotes_with_shlex(tmp_path):
    """The rendered text, for a reader of the hook file: shell-safe words stay bare."""
    body = git_hook._block("/srv/repos/app", "gitlab.com/acme/api", "/etc/cl.toml")
    assert "kb index /srv/repos/app --repo gitlab.com/acme/api" in body
    assert "--config /etc/cl.toml" in body
