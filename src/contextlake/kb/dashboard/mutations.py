"""Dashboard mutating actions: sync/add a repo, manage the HTTP-transport MCP server.

Kept separate from ``server.py`` so the actual side effects (subprocess calls, disk
writes) are unit-testable without an HTTP server in the loop. Every function here
takes an open store + store_dir and returns a plain ``dict`` -- ``server.py`` owns
the HTTP wiring, the token/Host auth, and the per-call store lock.

Every git/subprocess invocation passes an explicit argv list (never a shell string)
and, for anything derived from a request body, validates before it reaches the
argv -- a URL beginning with ``-`` would otherwise be parsed as a flag, and git's
``ext::`` transport is arbitrary command execution by design.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

_SAFE_NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_clone_url(url: str) -> str | None:
    """``None`` if ``url`` is safe to hand to ``git clone``; else a rejection reason.

    Allowlists the transports that can't be turned into command execution:
    ``https://``, ``ssh://``, and the scp-like ``user@host:path`` form. Rejects
    ``ext::`` (a literal arbitrary-command transport), ``file://`` (local-path
    disclosure), and anything starting with ``-`` (flag injection).
    """
    if not url or url.startswith("-"):
        return "URL must not be empty or start with '-'"
    if url.startswith("https://") or url.startswith("ssh://"):
        return None
    if re.match(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+$", url):
        return None
    return "unsupported URL (allowed: https://, ssh://, or user@host:path)"


def _derive_dest_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = tail.rsplit(":", 1)[-1]  # scp-like host:path form
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail


def _persist_repo(store, store_dir, repo_id: str, path: Path, head: str | None, shard) -> None:
    from ..model import Repo
    from ..state import mark_repo_indexed
    from ..store.shards import archive_shard, reindex_shard, write_shard

    store.upsert_repo(Repo(id=repo_id, path=str(path)))
    write_shard(store_dir, shard)
    archive_shard(store_dir, shard)
    reindex_shard(store, store_dir, repo_id)
    mark_repo_indexed(store, repo_id, head)


def sync_repo(store, store_dir, repo_id: str) -> dict:
    """``git pull --ff-only`` an already-indexed repo, then reindex if HEAD moved."""
    from ..cmds._common import _git_head
    from ..state import needs_reindex

    repo = store.get_repo(repo_id)
    if repo is None or not repo.path:
        return {"ok": False, "error": f"unknown repo: {repo_id}"}
    path = Path(repo.path)
    if not path.is_dir():
        return {"ok": False, "error": f"repo path missing on disk: {path}"}
    pull = subprocess.run(["git", "-C", str(path), "pull", "--ff-only"],
                         capture_output=True, text=True, timeout=120)
    if pull.returncode != 0:
        msg = (pull.stderr or pull.stdout or "git pull failed").strip()[:300]
        return {"ok": False, "error": msg}
    head = _git_head(path)
    if not needs_reindex(store, repo_id, head):
        return {"ok": True, "repo": repo_id, "changed": False, "message": "already up to date"}
    from ..parse import index_repo_dir  # lazy: tree-sitter
    shard = index_repo_dir(str(path), repo_id, head_commit=head)
    _persist_repo(store, store_dir, repo_id, path, head, shard)
    return {"ok": True, "repo": repo_id, "changed": True,
            "nodes": len(shard.nodes), "edges": len(shard.edges)}


def add_repo(store, store_dir, workspace, url: str) -> dict:
    """Clone ``url`` into ``workspace`` and index it, using the fleet's own
    canonical repo-id derivation (the remote URL) so the new repo behaves like
    every other one -- same dedup, same migration path."""
    reason = validate_clone_url(url)
    if reason:
        return {"ok": False, "error": reason}
    name = _derive_dest_name(url)
    if not name or not _SAFE_NAME_RX.match(name):
        return {"ok": False, "error": f"cannot derive a safe destination folder from {url!r}"}
    workspace = Path(workspace)
    dest = workspace / name
    if dest.exists():
        return {"ok": False, "error": f"destination already exists: {dest}"}
    workspace.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(["git", "clone", "--", url, str(dest)],
                          capture_output=True, text=True, timeout=300)
    if clone.returncode != 0:
        return {"ok": False, "error": (clone.stderr or "git clone failed").strip()[:300]}
    from ..cmds._common import _git_head
    from ..parse import index_repo_dir  # lazy: tree-sitter
    from ..repo_identity import resolve_repo_id
    repo_id = resolve_repo_id(str(dest))
    head = _git_head(dest)
    shard = index_repo_dir(str(dest), repo_id, head_commit=head)
    _persist_repo(store, store_dir, repo_id, dest, head, shard)
    return {"ok": True, "repo": repo_id, "path": str(dest),
            "nodes": len(shard.nodes), "edges": len(shard.edges)}


# --- MCP server (HTTP transport) lifecycle -----------------------------------

def _mcp_pidfile(store_dir) -> Path:
    return Path(store_dir) / "dashboard" / "mcp-server.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def mcp_status(store_dir) -> dict:
    pf = _mcp_pidfile(store_dir)
    if not pf.exists():
        return {"running": False}
    try:
        info = json.loads(pf.read_text())
    except (OSError, json.JSONDecodeError):
        return {"running": False}
    pid = info.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return {"running": False}
    return {"running": True, **info}


def mcp_start(store_dir, *, host: str = "127.0.0.1", port: int = 8766,
             config_path: str | None = None) -> dict:
    status = mcp_status(store_dir)
    if status["running"]:
        return {"ok": False, "error": "already running", **status}
    cmd = [sys.executable, "-m", "contextlake", "serve", "--transport", "http",
          "--host", host, "--port", str(port)]
    if config_path:
        cmd += ["--config", config_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    time.sleep(0.3)
    if proc.poll() is not None:
        return {"ok": False, "error": f"process exited immediately (code {proc.returncode})"}
    pf = _mcp_pidfile(store_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"pid": proc.pid, "host": host, "port": port}))
    return {"ok": True, "running": True, "pid": proc.pid, "host": host, "port": port}


def mcp_stop(store_dir) -> dict:
    status = mcp_status(store_dir)
    if not status["running"]:
        _mcp_pidfile(store_dir).unlink(missing_ok=True)
        return {"ok": True, "running": False, "message": "not running"}
    pid = status["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    _mcp_pidfile(store_dir).unlink(missing_ok=True)
    return {"ok": True, "running": False}


def mcp_restart(store_dir, **kw) -> dict:
    mcp_stop(store_dir)
    return mcp_start(store_dir, **kw)
