"""Git ``post-commit`` hook management — contextlake's continuous-intelligence path.

A per-repo hook re-indexes that repository into the knowledge store after every
commit, so the graph never drifts from HEAD without a manual ``index``/``bootstrap``.

These are pure file operations (install / uninstall / detect), driven by the
``hook`` CLI verb (:func:`contextlake.kb.commands.cmd_hook`), which resolves the
config, store, and canonical repo id and does the logging.
"""
from __future__ import annotations

import stat
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


def _block(repo_path: str, repo_id: str, config: str | None) -> str:
    cfg = f' --config "{config}"' if config else ""
    return (
        f"{MARK_BEGIN}\n"
        "# Re-index this repository into the contextlake knowledge store after each\n"
        "# commit. Detached (&) so the commit returns immediately. Managed by\n"
        "#   contextlake hook install / uninstall  — do not hand-edit.\n"
        f'( contextlake{cfg} index "{repo_path}" --repo "{repo_id}" '
        ">/dev/null 2>&1 & ) </dev/null\n"
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
    (added to a pre-existing hook), or ``not-a-repo``."""
    gd = git_dir(Path(repo_path))
    if gd is None:
        return "not-a-repo"
    hooks_dir = gd / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    block = _block(str(Path(repo_path).resolve()), repo_id, config)

    if hook.exists():
        existing = hook.read_text(encoding="utf-8")
        if MARK_BEGIN in existing:
            new = _strip_block(existing).rstrip("\n") + "\n" + block
            status = "refreshed"
        elif existing.strip():
            head = existing if existing.lstrip().startswith("#!") else "#!/bin/sh\n" + existing
            new = head.rstrip("\n") + "\n\n" + block
            status = "appended"
        else:
            new = "#!/bin/sh\n" + block
            status = "installed"
    else:
        new = "#!/bin/sh\n" + block
        status = "installed"

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
    gd = git_dir(Path(repo_path))
    if gd is None:
        return False
    hook = gd / "hooks" / "post-commit"
    return hook.exists() and MARK_BEGIN in hook.read_text(encoding="utf-8")
