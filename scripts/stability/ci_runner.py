"""Run contextlake for the stability harness on a GitHub Actions cell (tier F).

Why it exists: tier E's oracles take a run function. On a developer machine that function is
a private, guarded runner: it refuses live-state paths and puts stubs for every external tool
first on PATH. A CI cell has no live state, and the Windows cell cannot run shell stubs, so
this adapter keeps only what the results depend on:

- The environment is built from nothing. Only the keys named below pass, so the workflow's
  token never reaches contextlake.
- HOME points at the fake home the caller names. On Windows, `Path.home()` reads USERPROFILE,
  so USERPROFILE, HOMEDRIVE/HOMEPATH, APPDATA and LOCALAPPDATA point there too.
- PATH holds the target venv's scripts directory and one tool directory: on POSIX, symlinks
  to git and the shell tools the private runner allows; on Windows, git's own directory.
- On Windows the child writes UTF-8 to its pipes (PYTHONIOENCODING), the analog of the
  C.UTF-8 locale set for Linux. PYTHONUTF8 is NOT set: it would change contextlake's own
  file-reading default and hide a real Windows defect.
- The stub dir is created empty. Tier E reads no stub calls.

It refuses to do anything unless GITHUB_ACTIONS is "true". On a developer machine the
private runner is the only path, and this file must not become a way around its refusals.

Run as a script, it prints the tier F evidence lines (home, executables, file-system case,
line endings, path length, stubs, model snapshot)::

    python scripts/stability/ci_runner.py --venv VENV --root ROOT [--fixture WS] [--hf-cache DIR]
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"

# The same allow-list as the private runner, so a Linux cell runs under the same env.
_BASE_ENV = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HF_HUB_OFFLINE": "1",
             "TRANSFORMERS_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
# Windows processes need these to start: Python needs SYSTEMROOT for sockets and asyncio,
# and platform.machine() reads PROCESSOR_ARCHITECTURE. None of them is a secret.
_WINDOWS_PASS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE",
                 "NUMBER_OF_PROCESSORS")
# The tools the private runner puts on PATH (its allow dir), linked into one directory on
# POSIX so the rest of /usr/bin stays off PATH.
_POSIX_TOOLS = ("git", "sh", "bash", "env", "cat", "ls", "uname", "true", "false")
MODEL = "models--minishlab--potion-base-8M"

#: Set by tier_root(): the one directory besides the venv that PATH holds.
_TOOL_DIR: Path | None = None


class Refused(RuntimeError):
    """The adapter will not run here. The message names the rule."""


def require_ci() -> None:
    """Raise unless this process runs inside GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise Refused("ci_runner runs only inside GitHub Actions (GITHUB_ACTIONS=true). "
                      "On a developer machine, use the private guarded runner.")


@dataclasses.dataclass(frozen=True)
class Target:
    """One installed contextlake: a venv whose scripts directory holds the entry point."""

    name: str
    venv: Path

    @classmethod
    def from_venv(cls, venv: Path, name: str | None = None) -> Target:
        require_ci()
        venv = Path(venv).absolute()
        t = cls(name or venv.name, venv)
        if not t.exe.is_file():
            raise Refused(f"target has no contextlake entry point: {t.exe}")
        return t

    @property
    def bin_dir(self) -> Path:
        return self.venv / ("Scripts" if WINDOWS else "bin")

    def tool(self, name: str) -> Path:
        """An executable in the venv: ``Scripts\\<name>.exe`` on Windows, ``bin/<name>``."""
        p = self.bin_dir / name
        return p.with_name(name + ".exe") if WINDOWS and not name.endswith(".exe") else p

    @property
    def exe(self) -> Path:
        return self.tool("contextlake")

    @property
    def python(self) -> Path:
        return self.tool("python")


@dataclasses.dataclass
class Result:
    argv: list[str]
    rc: int | None
    out: str
    err: str
    seconds: float
    timed_out: bool
    env_keys: list[str]


def tier_root(base: Path) -> Path:
    """The working root. Keep it short (``$RUNNER_TEMP/f``): Windows paths stop at 260.

    Also sets up the tool directory that ``run`` puts on PATH, so call this first.
    """
    global _TOOL_DIR
    require_ci()
    root = Path(base).absolute()
    root.mkdir(parents=True, exist_ok=True)
    _TOOL_DIR = _tool_dir(root)
    return root


def _tool_dir(root: Path) -> Path:
    """POSIX: ``<root>/allow-bin``, symlinks to git and the shell tools listed above.
    Windows: git's own directory, which holds only git's programs."""
    git = shutil.which("git")
    if git is None:
        raise Refused("git is not on PATH; the fixture and `kb index` need it")
    if WINDOWS:
        return Path(git).parent
    d = root / "allow-bin"
    d.mkdir(exist_ok=True)
    for tool in _POSIX_TOOLS:
        real = shutil.which(tool)
        if real is None:
            continue
        link = d / tool
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(real)
    return d


def child_env(target: Target, home: Path, stub_dir: Path,
              extra: dict[str, str] | None = None) -> dict[str, str]:
    """The child's whole environment. Nothing from the parent passes unless named here."""
    if _TOOL_DIR is None:
        raise Refused("call tier_root() first: it sets up the tool directory PATH holds")
    env = dict(_BASE_ENV)
    if WINDOWS:
        env.update({k: os.environ[k] for k in _WINDOWS_PASS if k in os.environ})
        env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    tmp = str(home / "tmp")
    env.update({"HOME": str(home), "PATH": os.pathsep.join([str(target.bin_dir), str(_TOOL_DIR)]),
                "STAB_STUB_DIR": str(stub_dir), "TMPDIR": tmp,
                "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local/share")})
    if WINDOWS:
        drive, rest = os.path.splitdrive(str(home))
        env.update({"USERPROFILE": str(home), "HOMEDRIVE": drive, "HOMEPATH": rest or "\\",
                    "APPDATA": str(home / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(home / "AppData" / "Local"), "TEMP": tmp, "TMP": tmp})
    return env


def _text(b) -> str:
    if b is None:
        return ""
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b


def run(target: Target, args: list[str], *, home: Path, cwd: Path, stub_dir: Path,
        env: dict[str, str] | None = None, stdin: str | None = None,
        timeout: float = 120.0, exe: str | None = None,
        interrupt_after: float | None = None) -> Result:
    """Run ``contextlake <args>`` (or ``exe`` from the target venv). Same shape as the
    private runner's ``run``: every ``argv`` item is a ``str``, so callers can JSON it."""
    require_ci()
    if interrupt_after is not None and WINDOWS:
        raise NotImplementedError("interrupt_after sends SIGINT to a process group; "
                                  "Windows has no equivalent here and tier E does not use it")
    home, cwd, stub_dir = Path(home), Path(cwd), Path(stub_dir)
    prog = target.tool(exe) if exe else target.exe
    if not prog.is_file():
        raise Refused(f"no such executable in the target venv: {prog}")
    argv = [str(prog), *map(str, args)]
    child = child_env(target, home, stub_dir, env)
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    stub_dir.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    if interrupt_after is not None:
        return _run_interrupted(argv, cwd, child, stdin, timeout, interrupt_after, t0)
    try:
        # An empty stdin unless the caller gives one, so a prompt cannot block the job.
        p = subprocess.run(argv, cwd=cwd, env=child, input=stdin if stdin is not None else "",
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return Result(argv, p.returncode, p.stdout, p.stderr, time.monotonic() - t0, False,
                      sorted(child))
    except subprocess.TimeoutExpired as e:
        return Result(argv, None, _text(e.stdout), _text(e.stderr), time.monotonic() - t0,
                      True, sorted(child))


def _run_interrupted(argv, cwd, child, stdin, timeout, after, t0) -> Result:
    """POSIX only: SIGINT to the process group after ``after`` seconds, then collect."""
    p = subprocess.Popen(argv, cwd=cwd, env=child,
                         stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", start_new_session=True)
    if stdin:
        p.stdin.write(stdin)
        p.stdin.close()
    try:
        out, err = p.communicate(timeout=after)
        return Result(argv, p.returncode, out, err, time.monotonic() - t0, False, sorted(child))
    except subprocess.TimeoutExpired:
        pass
    os.killpg(p.pid, signal.SIGINT)
    try:
        out, err = p.communicate(timeout=timeout)
        return Result(argv, p.returncode, out, err, time.monotonic() - t0, False, sorted(child))
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        return Result(argv, None, out, err, time.monotonic() - t0, True, sorted(child))


# ======================================================================================
# Evidence: one line per Windows point, printed in the job log
# ======================================================================================
def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return p.stdout.strip() if p.returncode == 0 else f"<rc {p.returncode}: {p.stderr.strip()}>"


def evidence(target: Target, root: Path, fixture: Path | None, hf_cache: Path | None) -> None:
    probe = root / "evidence"
    home, cwd, stubs = probe / "home", probe / "cwd", probe / "stubs"
    print(f"evidence: platform {sys.platform}, harness python {sys.version.split()[0]}")
    print(f"evidence: target exe {target.exe} exists={target.exe.is_file()}")
    print(f"evidence: target python {target.python} exists={target.python.is_file()}")
    env = child_env(target, home, stubs)
    print(f"evidence: child env keys {sorted(env)}")
    print(f"evidence: child PATH {env['PATH']}")
    print(f"evidence: tool dir {_TOOL_DIR} holds {sorted(p.name for p in _TOOL_DIR.iterdir())}")
    if WINDOWS:
        print(f"evidence: child USERPROFILE {env['USERPROFILE']} HOMEDRIVE {env['HOMEDRIVE']} "
              f"HOMEPATH {env['HOMEPATH']}")
    r = run(target, ["-c", "from pathlib import Path; print(Path.home())"], home=home,
            cwd=cwd, stub_dir=stubs, exe="python", timeout=60)
    got = r.out.strip()
    print(f"evidence: Path.home() in the child = {got!r} (rc {r.rc}); fake home = {str(home)!r}; "
          f"match={got == str(home)}")
    if r.err.strip():
        print(f"evidence: child stderr {r.err.strip()[:300]!r}")
    case = probe / "case-probe"
    case.mkdir(parents=True, exist_ok=True)
    (case / "case.txt").write_bytes(b"x")
    print(f"evidence: file system under {root} is case-insensitive: "
          f"{(case / 'CASE.TXT').exists()}")
    print(f"evidence: stub dir {stubs} holds {sorted(p.name for p in stubs.iterdir())}")
    print(f"evidence: root {root} is {len(str(root))} characters")
    if hf_cache is not None:
        snaps = hf_cache / MODEL / "snapshots"
        names = sorted(p.name for p in snaps.iterdir()) if snaps.is_dir() else []
        print(f"evidence: model snapshots in {snaps}: {names}")
    if fixture is not None:
        for repo in sorted(p for p in fixture.iterdir() if (p / ".git").exists()):
            eol = collections.Counter(" ".join(line.split()[:2])
                                      for line in _git(repo, "ls-files", "--eol").splitlines())
            print(f"evidence: {repo.name}: core.autocrlf="
                  f"{_git(repo, 'config', '--local', '--get', 'core.autocrlf')} core.eol="
                  f"{_git(repo, 'config', '--local', '--get', 'core.eol')} "
                  f"head={_git(repo, 'rev-parse', 'HEAD')[:12]} ls-files --eol {dict(eol)}")
        tier_stubs = fixture.parent / "stubs"
        held = sorted(p.name for p in tier_stubs.iterdir()) if tier_stubs.is_dir() else None
        print(f"evidence: the tier's stub dir {tier_stubs} holds {held}")
        longest = max((str(p) for p in root.rglob("*")), key=len, default="")
        print(f"evidence: longest path under the root is {len(longest)} characters: {longest}")


def main(argv: list[str] | None = None) -> int:
    require_ci()
    ap = argparse.ArgumentParser(description="print the tier F evidence lines")
    ap.add_argument("--venv", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--fixture", help="a tier E workspace (<root>/<tag>/ws) to read git from")
    ap.add_argument("--hf-cache", help="the Hugging Face hub cache the model was saved to")
    a = ap.parse_args(argv)
    evidence(Target.from_venv(Path(a.venv)), tier_root(Path(a.root)),
             Path(a.fixture) if a.fixture else None, Path(a.hf_cache) if a.hf_cache else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
