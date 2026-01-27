"""CLI commands for the knowledge layer: index / query / serve / doctor.

Dispatched from the main ``gitlab-sync`` CLI. Imported lazily so the core sync
tool runs without the ``[kb]`` extra installed.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from ..logging_setup import log
from .config import load_kb_config
from .model import Repo
from .state import check_schema, mark_repo_indexed
from .store.shards import GraphShard, reindex_shard, write_shard
from .store.sqlite_store import SqliteStore

KB_VERBS = ("index", "serve", "query", "doctor")


def _open_store(args) -> tuple[SqliteStore, Path]:
    cfg = load_kb_config(getattr(args, "config", None))
    store_dir = cfg.store_path
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    return store, store_dir


def cmd_index(args) -> int:
    store, store_dir = _open_store(args)
    try:
        source = getattr(args, "source", None)
        if not source:
            log(f"Knowledge store ready at {store_dir} (no --source given; nothing indexed)")
            return 0
        try:
            raw = Path(source).read_text(encoding="utf-8")
        except OSError as e:
            log(f"Cannot read source {source!r}: {e}")
            return 1
        try:
            shard = GraphShard.model_validate_json(raw)
        except ValidationError as e:
            log(f"{source!r} is not a valid graph shard ({e.error_count()} error(s)); "
                "expected a JSON object with repo, nodes, and edges")
            return 1
        store.upsert_repo(Repo(id=shard.repo, path=str(Path(source).resolve())))
        write_shard(store_dir, shard)
        reindex_shard(store, store_dir, shard.repo)
        mark_repo_indexed(store, shard.repo, shard.head_commit)
        st = store.stats()
        log(f"Indexed {shard.repo}: {len(shard.nodes)} nodes, {len(shard.edges)} edges "
            f"(store totals: {st.nodes} nodes, {st.edges} edges)")
        return 0
    finally:
        store.close()


def cmd_query(args) -> int:
    text = " ".join(getattr(args, "args", []) or []).strip()
    if not text:
        log("usage: gitlab-sync query \"<text>\" [--kind K] [--repo R] [--limit N]")
        return 2
    store, _ = _open_store(args)
    try:
        results = store.search(
            text, kind=getattr(args, "kind", None), repo=getattr(args, "repo", None),
            limit=getattr(args, "limit", None) or 20,
        )
        if not results:
            log(f"No matches for {text!r}")
            return 0
        for n in results:
            loc = f"{n.file}:{n.line_start}" if n.file and n.line_start else (n.file or "?")
            log(f"  {n.repo} · {loc} · {n.kind} · {n.name}")
        return 0
    finally:
        store.close()


def cmd_serve(args) -> int:
    from .server import run_server  # imported here so `query`/`index` don't load it

    store, _ = _open_store(args)
    try:
        # CLI exposes "http"; the MCP SDK calls it "streamable-http".
        transport = "streamable-http" if getattr(args, "transport", None) == "http" else "stdio"
        host = getattr(args, "host", None) or "127.0.0.1"
        port = getattr(args, "port", None) or 8765
        log(f"Serving knowledge graph over MCP ({transport})")
        run_server(store, transport=transport, host=host, port=port)
        return 0
    finally:
        store.close()


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def cmd_doctor(args) -> int:
    print("gitlab-sync knowledge layer — doctor")
    ok = True

    fts = False
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.Error:
        fts = False
    ok &= _check("SQLite FTS5 available", fts, "" if fts else "search falls back to slower scans")

    ok &= _check("git on PATH", shutil.which("git") is not None)
    _check("glab on PATH (for syncing)", shutil.which("glab") is not None)  # advisory, not critical

    try:
        cfg = load_kb_config(getattr(args, "config", None))
        _check("config loads", True, f"{len(cfg.sources)} source(s), {len(cfg.rules)} rule(s)")
        store_dir = cfg.store_path
        store_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(store_dir / "index.sqlite")
        try:
            check_schema(store)
            st = store.stats()
            _check("store reachable", True,
                   f"{store_dir} · {st.repos} repos, {st.nodes} nodes, {st.edges} edges")
        finally:
            store.close()
    except Exception as e:  # noqa: BLE001 - doctor reports, never crashes
        ok &= _check("config + store", False, str(e))

    print("OK" if ok else "Problems found")
    return 0 if ok else 1


def dispatch(command: str, args) -> int:
    return {
        "index": cmd_index, "query": cmd_query, "serve": cmd_serve, "doctor": cmd_doctor,
    }[command](args)
