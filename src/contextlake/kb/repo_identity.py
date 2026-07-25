"""Canonical repository identity, independent of where it's checked out.

``repo_id`` used to be a repo's path relative to ``--workspace`` — the same
physical repo got a different id from a different index root, causing
duplicate ids and broken path-based owners/graph/impact views. This module
derives a stable id from the repo's ``origin`` remote instead, so it survives
being moved, re-cloned elsewhere, or indexed from a different workspace root.

Stdlib only (``subprocess`` + ``urllib.parse``), matching the rest of the `kb`
package's no-heavy-deps-in-the-core-path convention.
"""

from __future__ import annotations

import re
import subprocess
import urllib.parse
from pathlib import Path

__all__ = ["normalize_remote_url", "canonical_repo_id", "resolve_repo_id", "run_git"]

_GIT_TIMEOUT = 5.0
# scp-like syntax: user@host:path (the form `git@gitlab.com:group/project.git` uses)
_SCP_LIKE = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")


def normalize_remote_url(url: str) -> str:
    """``host/path``, lowercased, scheme/auth/``.git`` stripped.

    ``git@gitlab.com:acme/api.git`` and ``https://gitlab.com/acme/api.git`` (or
    a token-embedding ``https://x-token:abc@gitlab.com/acme/api.git``) all
    normalize to the same id, so the same project reached over SSH from one
    clone and HTTPS from another still lands on one node in the graph.
    """
    url = url.strip()
    m = _SCP_LIKE.match(url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.netloc.rsplit("@", 1)[-1]  # drop userinfo (user:token@)
        path = parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}/{path}".lower()


def run_git(path: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True,
                             text=True, timeout=_GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def canonical_repo_id(path: str) -> str | None:
    """The normalized ``origin`` remote URL, or ``None`` if the repo has none."""
    url = run_git(path, "remote", "get-url", "origin")
    return normalize_remote_url(url) if url else None


def _fallback_repo_id(path: str) -> str:
    """A stable id for a repo with no ``origin`` remote: its directory name plus
    a short hash of its root commit, so two differently-named local clones of
    the same history still collide (correctly), while two unrelated repos that
    happen to share a directory name (e.g. two different ``api``s) don't."""
    name = Path(path).name
    root = run_git(path, "rev-list", "--max-parents=0", "HEAD")
    first_root = root.splitlines()[0] if root else ""
    return f"{name}@{first_root[:12]}" if first_root else name


def resolve_repo_id(path: str) -> str:
    """The canonical id for the repo at ``path``: from its remote, else the
    directory-name+root-commit fallback for a remote-less repo."""
    return canonical_repo_id(path) or _fallback_repo_id(path)
