"""CLI commands for the knowledge layer: index / query / serve / doctor.

Dispatched from the main ``gitlab-sync`` CLI. Imported lazily so the core sync
tool runs without the ``[kb]`` extra installed.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

from pydantic import ValidationError

from ..logging_setup import log
from .config import load_kb_config
from .model import Repo
from .state import check_schema, mark_repo_indexed
from .store.shards import GraphShard, reindex_shard, write_shard
from .store.sqlite_store import SqliteStore

KB_VERBS = ("index", "connect", "serve", "query", "doctor")


def _open_store(args) -> tuple[SqliteStore, Path]:
    cfg = load_kb_config(getattr(args, "config", None))
    store_dir = cfg.store_path
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    return store, store_dir


def _git_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _store_and_index(store, store_dir, repo_id, repo_path, head, shard) -> int:
    store.upsert_repo(Repo(id=repo_id, path=str(repo_path)))
    write_shard(store_dir, shard)
    reindex_shard(store, store_dir, repo_id)
    mark_repo_indexed(store, repo_id, head)
    st = store.stats()
    log(f"Indexed {repo_id}: {len(shard.nodes)} nodes, {len(shard.edges)} edges "
        f"(store totals: {st.nodes} nodes, {st.edges} edges)")
    return 0


def _index_workspace(store, store_dir, workspace: Path) -> int:
    from .parse import discover_repos, index_repo_dir  # lazy: tree-sitter

    repos = discover_repos(str(workspace))
    if not repos:
        log(f"No git repositories found under {workspace}")
        return 0
    log(f"Found {len(repos)} repositories under {workspace}")
    failed = 0
    for i, (repo_id, path) in enumerate(repos, 1):
        try:
            head = _git_head(Path(path))
            shard = index_repo_dir(path, repo_id, head_commit=head)
            store.upsert_repo(Repo(id=repo_id, path=path))
            write_shard(store_dir, shard)
            reindex_shard(store, store_dir, repo_id)
            mark_repo_indexed(store, repo_id, head)
            log(f"  [{i}/{len(repos)}] {repo_id}: "
                f"{len(shard.nodes)} nodes, {len(shard.edges)} edges")
        except Exception as e:  # noqa: BLE001 - one repo must not abort the workspace
            failed += 1
            log(f"  [{i}/{len(repos)}] {repo_id}: FAILED — {e}")
    st = store.stats()
    log(f"Workspace indexed: {st.repos} repos, {st.nodes} nodes, {st.edges} edges "
        f"({failed} repo(s) failed)")
    return 0 if failed == 0 else 1


def cmd_index(args) -> int:
    store, store_dir = _open_store(args)
    try:
        workspace = getattr(args, "workspace", None)
        if workspace:
            return _index_workspace(store, store_dir, Path(workspace))

        source = getattr(args, "source", None)
        if not source:
            log(f"Knowledge store ready at {store_dir} (no --source given; nothing indexed)")
            return 0
        src = Path(source)

        if src.is_dir():
            from .parse import index_repo_dir  # lazy: only needs tree-sitter when indexing code

            repo_id = getattr(args, "repo", None) or src.name
            head = _git_head(src)
            shard = index_repo_dir(str(src), repo_id, head_commit=head)
            return _store_and_index(store, store_dir, repo_id, src.resolve(), head, shard)

        # otherwise treat --source as a graph-shard JSON file
        try:
            raw = src.read_text(encoding="utf-8")
        except OSError as e:
            log(f"Cannot read source {source!r}: {e}")
            return 1
        try:
            shard = GraphShard.model_validate_json(raw)
        except ValidationError as e:
            log(f"{source!r} is not a valid graph shard ({e.error_count()} error(s)); "
                "expected a JSON object with repo, nodes, and edges")
            return 1
        return _store_and_index(
            store, store_dir, shard.repo, src.resolve(), shard.head_commit, shard
        )
    finally:
        store.close()


def _rule_patterns(rules) -> tuple[str | None, list[str]]:
    """Pull the issue-key pattern and doc-link patterns out of configured rules."""
    branch_key = None
    link_patterns = []
    for r in rules:
        if r.type in ("branch_key", "issue_key") and r.pattern:
            branch_key = r.pattern
        elif r.type in ("link_scrape", "link") and r.pattern:
            link_patterns.append(r.pattern)
    return branch_key, link_patterns


def _connect_targets(args, store) -> list[tuple[str, str]]:
    """Repos to enrich: --workspace tree, a single --source dir, or all indexed."""
    workspace = getattr(args, "workspace", None)
    if workspace:
        from .parse import discover_repos  # lazy

        return discover_repos(str(workspace))
    source = getattr(args, "source", None)
    if source and Path(source).is_dir():
        repo_id = getattr(args, "repo", None) or Path(source).name
        return [(repo_id, str(Path(source).resolve()))]
    return [(r.id, r.path) for r in store.list_repos() if r.path]


def cmd_connect(args) -> int:
    from .connectors.orchestrate import build_atlassian, connect_partition, enrich_repo
    from .references import extract_issue_keys, scrape_links

    store, _ = _open_store(args)
    try:
        cfg = load_kb_config(getattr(args, "config", None))
        sources = [s for s in cfg.sources if s.type == "atlassian"]
        if not sources:
            log('No Atlassian sources configured (add [[sources]] type="atlassian" to kb.toml)')
            return 0
        branch_key, link_patterns = _rule_patterns(cfg.rules)
        if not branch_key and not link_patterns:
            log('No association rules configured (add [[rules]] type="branch_key"/"link_scrape")')
            return 0

        connectors = []
        for s in sources:
            conn = build_atlassian(s)
            try:
                sites = conn.discover_sites()
            except Exception as e:  # noqa: BLE001 - a dead source must not abort the run
                log(f"  source {s.name!r}: site discovery failed — {e}")
                continue
            log(f"  source {s.name!r}: {len(sites)} site(s) reachable")
            if sites:
                connectors.append((conn, sites))
        if not connectors:
            log("No reachable Atlassian sites; nothing to connect")
            return 1

        targets = _connect_targets(args, store)
        if not targets:
            log("No repos to enrich (index some first, or pass --workspace/--source)")
            return 0
        log(f"Enriching {len(targets)} repo(s) across {len(connectors)} source(s)")

        total_edges = 0
        for repo_id, path in targets:
            keys = extract_issue_keys(path, branch_key) if branch_key else []
            links = scrape_links(path, link_patterns) if link_patterns else []
            if not keys and not links:
                continue
            merged_nodes, merged_edges = {}, {}
            for conn, sites in connectors:
                try:
                    nodes, edges = enrich_repo(conn, sites, repo_id, issue_keys=keys, links=links)
                except Exception as e:  # noqa: BLE001 - one source/repo must not abort the run
                    log(f"  {repo_id}: source {conn.name!r} failed — {e}")
                    continue
                for n in nodes:
                    merged_nodes[n.id] = n
                for ed in edges:
                    merged_edges[(ed.src, ed.dst, ed.relation)] = ed
            part = connect_partition(repo_id)
            store.clear_repo(part)
            store.upsert_nodes(part, list(merged_nodes.values()))
            store.upsert_edges(part, list(merged_edges.values()))
            total_edges += len(merged_edges)
            if merged_edges:
                log(f"  {repo_id}: {len(merged_edges)} link(s)")
        log(f"Connect complete: {total_edges} external link(s) stored")
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
        "index": cmd_index, "connect": cmd_connect, "query": cmd_query,
        "serve": cmd_serve, "doctor": cmd_doctor,
    }[command](args)
