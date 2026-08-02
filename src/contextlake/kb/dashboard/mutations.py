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


# --- Wiki generation (live, from the dashboard) ------------------------------
#
# Single-repo or fleet-wide (repo_id=None runs every indexed repo, mirroring
# `contextlake wiki` with no args). Cluster/namespace generation is CLI-only --
# not exposed here; this covers what was actually asked for. Modeled on
# mcp_start/mcp_stop/mcp_status, not sync_repo/add_repo: an LLM-backed run has
# no safe fixed timeout (could be one repo or the whole fleet), so it's a
# non-blocking Popen + pidfile, not a blocking subprocess.run(timeout=...).

def _wiki_pidfile(store_dir) -> Path:
    return Path(store_dir) / "dashboard" / "wiki-gen.pid"


def _wiki_logfile(store_dir) -> Path:
    return Path(store_dir) / "dashboard" / "wiki-gen.log"


def _pid_finished(pid: int) -> bool:
    """True once ``pid`` has exited.

    ``wiki_generate_start`` never waits on its child (the point is to return
    immediately), so an exited child is a zombie until reaped -- and
    ``os.kill(pid, 0)`` reports a zombie as alive forever, since the pid is
    still allocated. Reap it via a non-blocking ``waitpid`` first (this IS
    our child, spawned by this same process); fall back to the plain
    alive-check for a pid that isn't our child (e.g. a dashboard restart lost
    track of a still-running prior run).

    ``ThreadingHTTPServer`` means two concurrent pollers (a second browser tab,
    a reload) can call this for the same pid at once -- benign: only one
    ``waitpid`` actually reaps it, the other gets ``ChildProcessError`` and
    falls through to ``_pid_alive``, which by then correctly reports False for
    the just-reaped pid.
    """
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return True
    except ChildProcessError:
        pass
    return not _pid_alive(pid)


def wiki_generate_status(store_dir) -> dict:
    """Poll a run started by :func:`wiki_generate_start`.

    Tails the log for live progress -- the CLI's own per-repo ``written``/
    ``rejected``/``unchanged`` lines, unchanged, so no new machine-readable
    progress format is needed. The log is read unconditionally, even after the
    pidfile is gone: the pidfile only tracks liveness and is cleared once a
    finished run has been reported, but the log is the durable record of what
    happened, including a failure -- exactly the case a poller most needs to
    still see, so a second/late poll (a reload, a second browser tab) must not
    go blank just because it missed the one ``finished: True`` tick.
    """
    log_path = _wiki_logfile(store_dir)
    tail = ""
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-40:])
    pf = _wiki_pidfile(store_dir)
    if not pf.exists():
        return {"running": False, "log_tail": tail}
    try:
        info = json.loads(pf.read_text())
    except (OSError, json.JSONDecodeError):
        return {"running": False, "log_tail": tail}
    pid = info.get("pid")
    running = isinstance(pid, int) and not _pid_finished(pid)
    result = {"running": running, "log_tail": tail,
             **{k: v for k, v in info.items() if k != "pid"}}
    if not running:
        pf.unlink(missing_ok=True)
        result["finished"] = True
    return result


def wiki_generate_estimate(store, store_dir, *, repo_id: str | None = None,
                           force: bool = False) -> dict:
    """Read-only, no LLM call: how many repos would actually regenerate.

    Replicates the COMMIT half of ``cmds.wiki.cmd_wiki``'s freshness-skip check
    (the ``at commit `<sha>``` provenance-footer regex) so the dashboard can
    show a real count before the user confirms a run. Deliberately not the
    whole check: ``cmd_wiki`` also regenerates a commit-unchanged page whose
    recorded subsystem pages no longer match the repo's current modules, and
    answering that here would mean running ``repo_modules`` per repo on an
    interactive request. So this is a lower bound -- a run can regenerate more
    than estimated, never fewer. With ``force``, every targeted repo counts as
    "would regenerate" -- the estimate makes that cost explicit instead of it
    being a surprise after the run has already started.
    """
    from ..wiki.generate import repo_brief

    if repo_id:
        r = store.get_repo(repo_id)
        repos = [r] if r else []
    else:
        repos = store.list_repos()
    wiki_dir = Path(store_dir) / "wiki"
    total = len(repos)
    would_regenerate = 0
    for r in repos:
        brief = repo_brief(store_dir, r.id)
        if brief is None:
            continue
        wiki_file = wiki_dir / (r.id.replace("/", "__") + ".md")
        stale_or_missing = True
        if not force and wiki_file.exists() and brief.get("head"):
            prev = wiki_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"at commit `([^`]+)`", prev)
            stale_or_missing = not (m and m.group(1) == brief["head"])
        if stale_or_missing:
            would_regenerate += 1
    return {"total": total, "would_regenerate": would_regenerate,
           "unchanged": total - would_regenerate}


def wiki_generate_start(store_dir, *, repo_id: str | None = None, force: bool = False,
                        llm: str | None = None, llm_model: str | None = None,
                        config_path: str | None = None) -> dict:
    """Spawn ``contextlake wiki`` as a subprocess -- reuses the exact, tested CLI
    generation loop unmodified, no duplicated logic. Refuses to start a second
    run while one is already in progress (single pidfile, same discipline as
    the MCP server lifecycle above).

    Unlike ``mcp_start`` (a server that should never legitimately exit within
    the startup grace period), a wiki run with nothing to do exits almost
    immediately with code 0 -- that's success, not a failure to detect, so
    there's no "exited immediately" check here; :func:`wiki_generate_status`
    reports it as ``finished`` on the very next poll.

    ``config_path`` should always be the caller's own resolved config path:
    without it, the subprocess falls back to the normal config discovery
    chain (global / ancestor ``.contextlake.kb.toml``), which may not be the
    same store this dashboard is serving.
    """
    status = wiki_generate_status(store_dir)
    if status["running"]:
        return {"ok": False, "error": "a wiki generation run is already in progress",
               **status}
    cmd = [sys.executable, "-m", "contextlake", "wiki"]
    if repo_id:
        cmd.append(repo_id)
    if force:
        cmd.append("--force")
    if llm:
        cmd += ["--llm", llm]
    if llm_model:
        cmd += ["--llm-model", llm_model]
    if config_path:
        cmd += ["--config", config_path]
    log_path = _wiki_logfile(store_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
    pf = _wiki_pidfile(store_dir)
    pf.write_text(json.dumps({"pid": proc.pid, "repo": repo_id, "force": force}))
    return {"ok": True, "running": True, "pid": proc.pid}
