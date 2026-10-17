"""Git ``post-commit`` hook management — contextlake's continuous-intelligence path.

A per-repo hook re-indexes that repository into the knowledge store after every
commit, so the graph never drifts from HEAD without a manual ``index``/``bootstrap``.

These are pure file operations (install / uninstall / detect), driven by the
``hook`` CLI verb (:func:`contextlake.kb.commands.cmd_hook`), which resolves the
config, store, and canonical repo id and does the logging.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

# A guarded block so we can refresh or remove our lines without clobbering a
# pre-existing user hook (we append to it, never overwrite it).
MARK_BEGIN = "# >>> contextlake (managed) — do not edit this block >>>"
MARK_END = "# <<< contextlake (managed) <<<"


def git_dir(repo_path: Path) -> Path | None:
    """The repo's git dir (where ``hooks/`` lives), or None if not a repo.

    Handles the plain ``.git`` directory and the worktree/submodule case where
    ``.git`` is a file containing ``gitdir: <path>``.
    """
    dot = repo_path / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        try:
            line = dot.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            gd = Path(line.split(":", 1)[1].strip())
            return gd if gd.is_absolute() else (repo_path / gd).resolve()
    return None


HOOK_LOG_NAME = "contextlake-index.log"
"""Where the detached re-index writes. Inside the git dir, so it travels with the
checkout, is ignored by git automatically, and is deleted with the repository."""


_GIT_TIMEOUT = 10

SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})
"""Interpreters the managed block is valid in. A hook with any other shebang (python, node)
would be handed shell text it cannot run."""

_ENDS = re.compile(r"^(exec|exit)\b", re.M)
"""A `exec` or `exit` at the start of a line: the script stops there, so a block placed
after it never runs."""

REFUSED = {
    "hooks-path": "git reads hooks from {where} (core.hooksPath), not from the repo's own "
                  ".git/hooks, so a hook written there would never run. That directory can "
                  "be shared with other repos, so nothing was written. Add the re-index "
                  "call there yourself, or unset core.hooksPath for this repo.",
    "blocked": "the existing post-commit hook has an exec or exit before its end, so a "
               "block added to it would never run. Nothing was changed. Move that line to "
               "the end of the hook, or add the re-index call above it yourself.",
    "not-shell": "the existing post-commit hook is not a shell script, so the shell block "
                 "cannot be added to it. Nothing was changed.",
}
"""Statuses that mean no hook was written. `install` returns one of these keys, and
:func:`refusal` turns it into the sentence the user reads."""


def _git(repo_path: Path, gd: Path, *args: str) -> str:
    """Stdout of a git command pinned to ``gd`` with ``repo_path`` as the working directory.

    ``--git-dir`` and not ``-C``: with a broken ``.git``, ``-C`` walks up and answers for a
    parent repository (see ``repo_identity.is_own_gitdir``). Empty when git cannot be run
    or exits non-zero, which callers read as "no override".
    """
    try:
        out = subprocess.run(["git", "--git-dir", str(gd), *args], cwd=repo_path,
                             capture_output=True, text=True, errors="replace",
                             timeout=_GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def hooks_path_override(repo_path: Path, gd: Path) -> Path | None:
    """Where ``core.hooksPath`` sends git for hooks, when that is not ``<gitdir>/hooks``.

    Husky, lefthook and global hook guards set it. Git then ignores ``<gitdir>/hooks``,
    so a hook written there is never run, and the directory it names can belong to
    every repo on the machine. None when the setting is absent or points at the repo's own
    hooks directory.
    """
    if not _git(repo_path, gd, "config", "--get", "core.hooksPath"):
        return None
    # `rev-parse --git-path` expands `~` and applies the relative-path rule the way git does.
    where = _git(repo_path, gd, "rev-parse", "--git-path", "hooks")
    if not where:
        return None
    path = (repo_path / where).resolve()
    return None if path == (gd / "hooks").resolve() else path


def _interpreter(text: str) -> str | None:
    """The program named on the shebang line, or None when the text has no shebang."""
    first = text.split("\n", 1)[0]
    if not first.startswith("#!"):
        return None
    words = first[2:].split()
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    return os.path.basename(words[0]) if words else ""


def _place_block(rest: str, block: str, sep: str) -> str | None:
    """``rest`` (a hook without our block) with the block added where it will run.

    At the end, or directly above a closing `exec`/`exit` line that has only blanks and
    comments after it. None when an `exec`/`exit` sits earlier in the script, because the
    block would be skipped or would land inside whatever construct that line belongs to.
    ``sep`` is the gap before a block at the end of the file, kept as it was so an
    existing hook is rewritten with the same bytes.
    """
    lines = rest.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _ENDS.match(line):
            if any(t.strip() and not t.lstrip().startswith("#") for t in lines[i + 1:]):
                return None
            return "".join(lines[:i]).rstrip("\n") + "\n\n" + block + "\n" + "".join(lines[i:])
    return rest.rstrip("\n") + sep + block


def _runs(text: str) -> bool:
    """Whether git would reach our block in this hook: a shell script that does not
    `exec` or `exit` above it."""
    if _interpreter(text) not in (None, *SHELLS):
        return False
    return _ENDS.search(text[:text.index(MARK_BEGIN)]) is None


def refusal(repo_path: str, status: str) -> str:
    """The sentence that explains one of the :data:`REFUSED` statuses."""
    where = ""
    if status == "hooks-path":
        gd = git_dir(Path(repo_path))
        found = hooks_path_override(Path(repo_path), gd) if gd else None
        where = str(found) if found else "another directory"
    return REFUSED[status].format(where=where)


def _block(repo_path: str, repo_id: str, config: str | None) -> str:
    """The managed `post-commit` body.

    Two things here are deliberate and were both wrong before.

    **The command is the running interpreter, not a bare ``contextlake``.** Git runs
    hooks with a minimal environment, and on a venv install the console script is not on
    that PATH. Measured: the hook ran, the commit succeeded, the store head never moved,
    and ``kb hook status`` still reported the hook as present -- a failure invisible from
    every angle a user would check. See ``contextlake.launcher``; this file is
    machine-local, so it takes the always-correct spelling rather than the portable one.

    **Output goes to a log, not to ``/dev/null``.** The previous body sent stdout *and*
    stderr to ``/dev/null``, so an indexing error left no trace anywhere. That is the
    same defect ``cmds/refresh.py`` names in its own docstring -- "a background process
    with nowhere to write its errors fails invisibly, which is the class of bug this
    whole command exists to fight" -- and this hook was the counter-example to it.
    """
    from ..launcher import launch_command

    # `shlex.quote` on every value that is not ours. Double quotes were here before, and
    # inside them the shell still runs `$(...)`, backticks and `$VAR`. The repo path is a
    # directory name and the repo id comes from a remote URL, so either can carry them, and
    # the hook runs on every commit with the user's rights. `launch_command()` is already
    # quoted; `cmds/refresh.py` quotes its copy of this command the same way.
    cfg = f" --config {shlex.quote(config)}" if config else ""
    return (
        f"{MARK_BEGIN}\n"
        "# Re-index this repository into the contextlake knowledge store after each\n"
        "# commit. Detached (&) so the commit returns immediately. Managed by\n"
        "#   contextlake kb hook install / uninstall  — do not hand-edit.\n"
        f'# Errors land in $(git rev-parse --git-dir)/{HOOK_LOG_NAME}.\n'
        f'_cl_log="$(git rev-parse --git-dir)/{HOOK_LOG_NAME}"\n'
        f"( {launch_command()}{cfg} kb index {shlex.quote(repo_path)} "
        f"--repo {shlex.quote(repo_id)} "
        '>>"$_cl_log" 2>&1 & ) </dev/null\n'
        f"{MARK_END}\n"
    )


def _strip_block(text: str) -> str:
    """Return ``text`` with our managed block (inclusive of markers) removed."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line.startswith(MARK_BEGIN):
            skipping = True
            continue
        if skipping:
            if line.startswith(MARK_END):
                skipping = False
            continue
        out.append(line)
    return "".join(out)


def install(repo_path: str, repo_id: str, config: str | None = None) -> str:
    """Install or refresh the post-commit hook. Returns a status word:
    ``installed`` (new file), ``refreshed`` (our block updated), ``appended``
    (added to a pre-existing hook), ``not-a-repo``, or one of :data:`REFUSED` when
    no hook was written."""
    gd = git_dir(Path(repo_path))
    if gd is None:
        return "not-a-repo"
    if hooks_path_override(Path(repo_path), gd) is not None:
        return "hooks-path"
    hooks_dir = gd / "hooks"
    hook = hooks_dir / "post-commit"
    block = _block(str(Path(repo_path).resolve()), repo_id, config)

    if hook.exists():
        existing = hook.read_text(encoding="utf-8")
        if MARK_BEGIN in existing:
            rest, sep, status = _strip_block(existing), "\n", "refreshed"
        elif existing.strip():
            rest = existing if existing.lstrip().startswith("#!") else "#!/bin/sh\n" + existing
            sep, status = "\n\n", "appended"
        else:
            rest, sep, status = "#!/bin/sh\n", "\n", "installed"
        if _interpreter(rest) not in (None, *SHELLS):
            return "not-shell"
        new = _place_block(rest, block, sep)
        if new is None:
            return "blocked"
    else:
        new = "#!/bin/sh\n" + block
        status = "installed"

    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook.write_text(new, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return status


def uninstall(repo_path: str) -> str:
    """Remove our managed block. Returns ``removed``, ``absent``, or ``not-a-repo``.
    Deletes the hook file only if nothing but a shebang remains (i.e. it was ours)."""
    gd = git_dir(Path(repo_path))
    if gd is None:
        return "not-a-repo"
    hook = gd / "hooks" / "post-commit"
    if not hook.exists() or MARK_BEGIN not in hook.read_text(encoding="utf-8"):
        return "absent"
    stripped = _strip_block(hook.read_text(encoding="utf-8"))
    if stripped.strip() in ("", "#!/bin/sh"):
        hook.unlink()
    else:
        hook.write_text(stripped, encoding="utf-8")
    return "removed"


def is_installed(repo_path: str) -> bool:
    """Whether a commit in this repo will run our block, not merely whether the file has it."""
    gd = git_dir(Path(repo_path))
    if gd is None or hooks_path_override(Path(repo_path), gd) is not None:
        return False
    hook = gd / "hooks" / "post-commit"
    if not hook.exists():
        return False
    text = hook.read_text(encoding="utf-8")
    return MARK_BEGIN in text and _runs(text)
