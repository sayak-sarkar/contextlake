"""CLI commands for the knowledge layer: index / query / serve / doctor.

Dispatched from the main ``contextlake`` CLI. Imported lazily so the core sync
tool runs without the ``[kb]`` extra installed.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from pydantic import ValidationError

from .. import style
from ..logging_setup import log
from .config import apply_llm_overrides, load_kb_config
from .model import Repo
from .state import check_schema, mark_repo_indexed, needs_reindex
from .store.shards import GraphShard, archive_shard, reindex_shard, write_shard
from .store.sqlite_store import SqliteStore


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
    archive_shard(store_dir, shard)
    reindex_shard(store, store_dir, repo_id)
    mark_repo_indexed(store, repo_id, head)
    st = store.stats()
    log(f"Indexed {repo_id}: {len(shard.nodes)} nodes, {len(shard.edges)} edges "
        f"(store totals: {st.nodes} nodes, {st.edges} edges)")
    return 0


def _watch_loop(run_once, *, interval: float = 60, iterations=None, sleep=time.sleep) -> int:
    """Run ``run_once`` every ``interval`` seconds until interrupted or ``iterations``
    is reached. Returns the number of runs; a KeyboardInterrupt stops gracefully."""
    runs = 0
    while iterations is None or runs < iterations:
        run_once()
        runs += 1
        if iterations is not None and runs >= iterations:
            break
        try:
            sleep(interval)
        except KeyboardInterrupt:
            break
    return runs


def _default_index_workers() -> int:
    return min(8, max(1, (os.cpu_count() or 2) - 1))


def _index_workspace(store, store_dir, workspace: Path, *, force: bool = False,
                     skip_generated: bool = True, max_file_bytes: int | None = None,
                     workers: int | None = None, repo_filter: str | None = None) -> int:
    from .parse import DEFAULT_MAX_FILE_BYTES, discover_repos, index_repo_dir  # lazy: tree-sitter

    if max_file_bytes is None:
        max_file_bytes = DEFAULT_MAX_FILE_BYTES
    repos = discover_repos(str(workspace))
    # --repos scopes indexing to a subset (so `bootstrap --repos ...` indexes only the
    # mirrored subset, even when the workspace holds the full fleet).
    if repo_filter:
        from ..core import _repo_filter_patterns, match_repo_filter
        patterns = _repo_filter_patterns({"repo_filter": repo_filter})
        if patterns:
            repos = [(rid, path) for rid, path in repos
                     if match_repo_filter(rid, rid, patterns)]
    if not repos:
        # An empty workspace must fail loudly: an agent cannot cite from an empty
        # graph, so "success" here would be the hollow kind.
        extra = f" matching --repos {repo_filter!r}" if repo_filter else ""
        log(style.warn(f"No git repositories found under {workspace}{extra} — "
                       "nothing indexed."))
        log("  Point --workspace at a directory that contains git clones.")
        return 1
    mode = "full" if force else "incremental"
    failed = skipped = done = 0

    # Incremental filter first (cheap serial DB reads): only repos whose HEAD moved.
    todo = []
    for repo_id, path in repos:
        head = _git_head(Path(path))
        if not force and not needs_reindex(store, repo_id, head):
            skipped += 1
        else:
            todo.append((repo_id, path, head))
    total = len(todo)
    if workers is None or workers <= 0:
        workers = _default_index_workers()
    log(f"Found {len(repos)} repositories under {workspace} ({mode}); "
        f"indexing {total} with {workers} worker(s)")

    def _persist(repo_id, path, head, shard):
        store.upsert_repo(Repo(id=repo_id, path=path))
        write_shard(store_dir, shard)
        archive_shard(store_dir, shard)
        reindex_shard(store, store_dir, repo_id)
        mark_repo_indexed(store, repo_id, head)

    def _report(repo_id, shard):
        nonlocal done
        done += 1
        log(f"  {style.bar(done, total, 14)} {style.cyan(repo_id)}: "
            f"{len(shard.nodes)} nodes, {len(shard.edges)} edges")

    def _run_serial(items):
        nonlocal failed
        for repo_id, path, head in items:
            try:
                shard = index_repo_dir(path, repo_id, head_commit=head,
                                       skip_generated=skip_generated, max_file_bytes=max_file_bytes)
            except Exception as e:  # noqa: BLE001 - one repo must not abort the workspace
                failed += 1
                log(f"  {style.fail(repo_id)}: {e}")
                continue
            _persist(repo_id, path, head, shard)
            _report(repo_id, shard)

    if workers <= 1 or total <= 1:
        _run_serial(todo)
    else:
        # Parse repos in parallel (CPU-bound); persist serially here, since SQLite
        # must be written from a single process. Use the `spawn` start method on
        # every platform so behaviour and efficiency are identical on Linux, macOS
        # and Windows (Windows has only spawn; macOS defaults to it; on Linux spawn
        # benchmarks the same as fork). Workers re-import the package, which is safe
        # because every entry point is __main__-guarded; the per-worker startup is a
        # one-time cost amortised across the whole repo set.
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from concurrent.futures.process import BrokenProcessPool

        ctx = mp.get_context("spawn")
        try:
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
                futs = {
                    ex.submit(index_repo_dir, path, repo_id, head, None,
                              max_file_bytes=max_file_bytes, skip_generated=skip_generated):
                        (repo_id, path, head)
                    for repo_id, path, head in todo
                }
                for fut in as_completed(futs):
                    repo_id, path, head = futs[fut]
                    try:
                        shard = fut.result()
                    except Exception as e:  # noqa: BLE001 - one repo must not abort the workspace
                        failed += 1
                        log(f"  {style.fail(repo_id)}: {e}")
                        continue
                    _persist(repo_id, path, head, shard)
                    _report(repo_id, shard)
        except (BrokenProcessPool, OSError) as e:
            # The worker pool could not run here (sandboxed env, no fork/spawn, …).
            # Re-run the full work-list serially — persist is upsert-based and
            # idempotent, so repos already written simply update in place.
            log(f"{style.warn()} Parallel indexing unavailable ({e}); "
                f"falling back to serial.")
            done = 0
            _run_serial(todo)
    st = store.stats()
    glyph = style.ok() if failed == 0 else style.warn()
    log(f"{glyph} Workspace indexed: {st.repos} repos, {st.nodes} nodes, "
        f"{st.edges} edges ({skipped} unchanged, {failed} failed)")
    return 0 if failed == 0 else 1


def cmd_index(args) -> int:
    store, store_dir = _open_store(args)
    cfg = load_kb_config(getattr(args, "config", None))
    parse_opts = dict(skip_generated=cfg.skip_generated, max_file_bytes=cfg.max_file_bytes)
    workers = cfg.index_workers
    try:
        workspace = getattr(args, "workspace", None)
        if workspace:
            force = getattr(args, "force", False)
            repo_filter = getattr(args, "repos", None)
            if getattr(args, "watch", False):
                interval = getattr(args, "interval", None) or 60
                log(f"{style.cyan('watch')}: re-indexing {workspace} every "
                    f"{interval}s (Ctrl-C to stop)")
                _watch_loop(
                    lambda: _index_workspace(store, store_dir, Path(workspace),
                                             force=force, workers=workers,
                                             repo_filter=repo_filter, **parse_opts),
                    interval=interval,
                )
                return 0
            return _index_workspace(store, store_dir, Path(workspace),
                                    force=force, workers=workers,
                                    repo_filter=repo_filter, **parse_opts)

        # `contextlake index PATH` and `index --source PATH` are the same thing.
        source = getattr(args, "source", None) or getattr(args, "path", None)
        if not source:
            # Zero-config: with no path/--source/--workspace, index the current
            # directory so `cd my-repo && contextlake index` just works.
            source = "."
            log(f"No --source/--workspace given; indexing the current directory "
                f"({Path(source).resolve()}). Pass --source PATH or --workspace DIR "
                f"to index elsewhere.")
        src = Path(source)

        if src.is_dir():
            from .parse import index_repo_dir  # lazy: only needs tree-sitter when indexing code

            repo_id = getattr(args, "repo", None) or src.resolve().name  # "." -> cwd name
            head = _git_head(src)
            shard = index_repo_dir(str(src), repo_id, head_commit=head, **parse_opts)
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
    """Pull the issue-key pattern and doc-link patterns out of configured rules.

    A ``link_scrape`` rule may carry a single ``pattern`` or a ``patterns`` list
    (the latter is what the example config uses); both are accepted.
    """
    branch_key = None
    link_patterns = []
    for r in rules:
        extra = getattr(r, "model_extra", None) or {}
        if r.type in ("branch_key", "issue_key") and r.pattern:
            branch_key = r.pattern
        elif r.type in ("link_scrape", "link"):
            if r.pattern:
                link_patterns.append(r.pattern)
            link_patterns.extend(
                p for p in (extra.get("patterns") or []) if isinstance(p, str)
            )
    return branch_key, link_patterns


def _connect_targets(args, store) -> list[tuple[str, str]]:
    """Repos to enrich: --workspace tree, a single --source dir, else indexed repos —
    scoped to positional repo id(s) when given (``wiki <repo>`` does just that repo)."""
    workspace = getattr(args, "workspace", None)
    if workspace:
        from .parse import discover_repos  # lazy

        return discover_repos(str(workspace))
    source = getattr(args, "source", None)
    if source and Path(source).is_dir():
        repo_id = getattr(args, "repo", None) or Path(source).name
        return [(repo_id, str(Path(source).resolve()))]
    targets = [(r.id, r.path) for r in store.list_repos() if r.path]
    wanted = {a for a in (getattr(args, "args", None) or []) if a}
    if wanted:
        targets = [t for t in targets if t[0] in wanted]
    return targets


def _build_enrichers(sources):
    """Turn configured sources into callables ``fn(repo_id, keys, links)`` that
    return ``(nodes, edges)``. Atlassian sources discover their sites up front;
    Figma sources need no discovery. Returns ``(enrichers, names)``."""
    from .connectors.orchestrate import (
        build_atlassian,
        build_figma,
        build_gitlab,
        enrich_repo,
        enrich_repo_figma,
        enrich_repo_gitlab,
    )

    enrichers, names = [], []
    for s in sources:
        if s.type == "atlassian":
            conn = build_atlassian(s)
            try:
                sites = conn.discover_sites()
            except Exception as e:  # noqa: BLE001 - a dead source must not abort the run
                log(f"  source {s.name!r}: site discovery failed — {e}")
                continue
            log(f"  source {s.name!r} (atlassian): {len(sites)} site(s) reachable")
            if not sites:
                continue
            enrichers.append(
                lambda repo_id, keys, links, c=conn, st=sites:
                enrich_repo(c, st, repo_id, issue_keys=keys, links=links)
            )
            names.append(s.name)
        elif s.type == "figma":
            conn = build_figma(s)
            log(f"  source {s.name!r} (figma): ready")
            enrichers.append(
                lambda repo_id, keys, links, c=conn:
                enrich_repo_figma(c, repo_id, links=links)
            )
            names.append(s.name)
        elif s.type == "gitlab":
            conn = build_gitlab(s)
            log(f"  source {s.name!r} (gitlab): ready")
            enrichers.append(
                lambda repo_id, keys, links, c=conn: enrich_repo_gitlab(c, repo_id)
            )
            names.append(s.name)
    return enrichers, names


def cmd_connect(args) -> int:
    from .connectors.orchestrate import connect_partition
    from .references import extract_issue_keys, scrape_links

    store, _ = _open_store(args)
    try:
        cfg = load_kb_config(getattr(args, "config", None))
        sources = [s for s in cfg.sources if s.type in ("atlassian", "figma", "gitlab")]
        if not sources:
            log('No connector sources configured '
                '(add [[sources]] type="atlassian"/"figma"/"gitlab")')
            return 0
        has_gitlab = any(s.type == "gitlab" for s in sources)
        branch_key, link_patterns = _rule_patterns(cfg.rules)
        if not branch_key and not link_patterns and not has_gitlab:
            log('No association rules configured (add [[rules]] type="branch_key"/"link_scrape")')
            return 0

        enrichers, names = _build_enrichers(sources)
        if not enrichers:
            log("No usable connector sources; nothing to connect")
            return 1

        def _connect_once() -> int:
            targets = _connect_targets(args, store)
            if not targets:
                log("No repos to enrich (index some first, or pass --workspace/--source)")
                return 0
            log(f"Enriching {len(targets)} repo(s) across "
                f"{len(enrichers)} source(s): {', '.join(names)}")

            total_edges = 0
            attempts = src_failed = 0
            for repo_id, path in targets:
                keys = extract_issue_keys(path, branch_key) if branch_key else []
                links = scrape_links(path, link_patterns) if link_patterns else []
                if not keys and not links and not has_gitlab:
                    continue  # GitLab sources fetch by repo, so don't skip when one exists
                merged_nodes, merged_edges = {}, {}
                for name, enrich in zip(names, enrichers):
                    attempts += 1
                    try:
                        nodes, edges = enrich(repo_id, keys, links)
                    except Exception as e:  # noqa: BLE001 - one source/repo must not abort the run
                        log(f"  {repo_id}: source {name!r} failed — {e}")
                        src_failed += 1
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
            # Honest exit: every source call attempted failed (e.g. an unreachable
            # connector) -> a failure, even though per-repo errors were logged.
            if attempts and src_failed == attempts:
                log(style.warn(f"All {attempts} source call(s) failed — no links stored"))
                return 1
            return 0

        if getattr(args, "watch", False):
            interval = getattr(args, "interval", None) or 60
            log(f"{style.cyan('watch')}: re-connecting every {interval}s (Ctrl-C to stop)")
            _watch_loop(_connect_once, interval=interval)
            return 0
        return _connect_once()
    finally:
        store.close()


def _embed_unavailable_hint(cfg) -> str:
    """Why is there no embedder, and the exact one-liner to fix it.

    `build_embedder` returns None either because semantic search is off (the
    opt-in default) or because it is on but no engine is installed/reachable.
    Either way the old "Embeddings are disabled" message left the user stuck — so
    spell out the install + enable steps, tailored to what is actually missing.
    """
    import importlib.util

    has_local = importlib.util.find_spec("model2vec") is not None
    if not getattr(cfg, "enabled", False):
        install = ("" if has_local
                   else "install the embedder (pip install 'contextlake[kb-full]'), then ")
        return ("Semantic search is off (opt-in). To enable it: " + install
                + 'set [embeddings] enabled = true in kb.toml (provider "auto"). '
                "The first embed downloads a small CPU model (~30MB, one-time).")
    if not has_local:
        return ("Embeddings are enabled but no embedder is installed — "
                "install one with: pip install 'contextlake[kb-full]' (built-in CPU model), "
                "or run a local Ollama.")
    return ("Embeddings are enabled but no embedder resolved (no local Ollama, and the built-in "
            "engine failed to load). Run `contextlake doctor` for details.")


def cmd_embed(args) -> int:
    from .embeddings import build_embedder
    from .embeddings.index import embed_repo
    from .embeddings.store import build_vector_store

    store, store_dir = _open_store(args)
    try:
        cfg = load_kb_config(getattr(args, "config", None))
        embedder = build_embedder(cfg.embeddings)
        if embedder is None:
            log(_embed_unavailable_hint(cfg.embeddings))
            return 0
        targets = _connect_targets(args, store)
        if not targets:
            log("No indexed repos to embed (run index first, or pass --workspace/--source)")
            return 0
        limit = getattr(args, "limit", None)
        vs = build_vector_store(store_dir / "embeddings.sqlite",
                                backend=cfg.embeddings.vector_backend,
                                chunk_size=cfg.embeddings.vector_chunk_size)
        try:
            # Guard against re-embedding this store with a different model/dim (the
            # brute search silently drops mismatched dims). Probe the dim once; a
            # network failure here is left for the per-repo loop to report.
            from .embeddings.store import guard_store_identity
            try:
                probe = embedder.embed(["contextlake"])
            except Exception:  # noqa: BLE001 - unreachable embedder; loop reports it
                probe = None
            if probe and probe[0]:
                identity = getattr(embedder, "identity", None) or getattr(
                    embedder, "name", "embedder")
                guard_store_identity(vs, identity, len(probe[0]))
            # Incremental: skip a repo whose indexed HEAD hasn't moved since it was
            # last embedded. `--force` re-embeds; `--limit` (partial) never gates.
            from .embeddings.index import EMBED_CONTENT_VERSION
            from .embeddings.store import (
                get_content_version,
                get_embedded_head,
                set_content_version,
                set_embedded_head,
            )
            force = getattr(args, "force", False)
            incremental = limit is None and not force
            # A node->text mapping change makes every stored vector stale regardless
            # of HEADs: one intentional full re-embed, then incremental resumes.
            stored_cv = get_content_version(vs)
            if incremental and vs.count() and stored_cv != EMBED_CONTENT_VERSION:
                log(f"Embedding text format changed (v{stored_cv} -> "
                    f"v{EMBED_CONTENT_VERSION}) — re-embedding everything once")
                incremental = False

            def _embed_once() -> int:
                # Re-resolve targets each pass so `--watch` picks up newly indexed repos.
                pass_targets = _connect_targets(args, store)
                if not pass_targets:
                    log("No indexed repos to embed (run index first, or pass --workspace/--source)")
                    return 0
                log(f"Embedding {len(pass_targets)} repo(s) with {embedder.name} "
                    f"into the {vs.name} vector store")
                total = failed = skipped = 0
                for repo_id, _ in pass_targets:
                    repo = store.get_repo(repo_id)
                    head = repo.head_commit if repo else None
                    if incremental and head and get_embedded_head(vs, repo_id) == head:
                        skipped += 1
                        continue
                    try:
                        n = embed_repo(store_dir, vs, embedder, repo_id,
                                       batch_size=cfg.embeddings.batch_size, limit=limit)
                    except Exception as e:  # noqa: BLE001 - one repo must not abort the run
                        log(f"  {repo_id}: embed failed — {e}")
                        failed += 1
                        continue
                    if limit is None:
                        set_embedded_head(vs, repo_id, head)
                    total += n
                    if n:
                        log(f"  {repo_id}: embedded {n} node(s)")
                tail = f", {skipped} already up to date" if skipped else ""
                log(f"Embed complete: {total} vector(s) written "
                    f"({vs.count()} total in store){tail}")
                # Honest exit: if every repo we actually tried to embed failed (e.g. the
                # embedder went unreachable mid-run), this is a failure, not success.
                # Up-to-date repos that were skipped don't count as attempts.
                attempted = len(pass_targets) - skipped
                if attempted and failed == attempted:
                    log(style.warn(
                        f"Embed failed for all {attempted} repo(s) — no vectors written"))
                    return 1
                # Only a full, failure-free pass earns the new content-version stamp;
                # a partial (--limit) or partly-failed pass must stay marked stale.
                if limit is None and failed == 0:
                    set_content_version(vs, EMBED_CONTENT_VERSION)
                return 0

            if getattr(args, "watch", False):
                interval = getattr(args, "interval", None) or 60
                log(f"{style.cyan('watch')}: re-embedding every {interval}s (Ctrl-C to stop)")
                _watch_loop(_embed_once, interval=interval)
                return 0
            return _embed_once()
        finally:
            vs.close()
    finally:
        store.close()


def _wiki_partition(repo_id: str) -> str:
    """Store partition holding a repo's wiki-page sections (advisory prose)."""
    return f"@wiki:{repo_id}"


def _wiki_section_nodes(repo_id: str, page: str, filename: str):
    """Split a wiki page into ``##`` sections -> (nodes, texts) for the ``@wiki``
    partition. Sections embed as advisory prose alongside the code vectors
    (mirroring the ``@connect``/``@ingest`` partition pattern), so a
    natural-language question can land on the wiki's explanation and still cite
    the page file. Ids are per-section-index, so a regenerated page cleanly
    replaces its predecessor."""
    from .model import Node

    part = _wiki_partition(repo_id)
    sections, title, buf = [], "Overview", []
    for line in page.splitlines():
        if line.startswith("## "):
            if "\n".join(buf).strip():
                sections.append((title, "\n".join(buf).strip()))
            title, buf = (line[3:].strip() or "Section"), []
        else:
            buf.append(line)
    if "\n".join(buf).strip():
        sections.append((title, "\n".join(buf).strip()))
    nodes, texts = [], []
    for i, (t, body) in enumerate(sections):
        nodes.append(Node(id=f"{part}:{i}", repo=part, kind="wiki",
                          name=f"{repo_id} wiki: {t}", file=f"wiki/{filename}",
                          attrs={"advisory": True, "source_repo": repo_id}))
        texts.append(f"{t}\n{body}")
    return nodes, texts


def _store_wiki_partition(store, store_dir, repo_id, page, filename, head,
                          embedder=None, vs=None, batch_size=64) -> int:
    """(Re)write a repo's ``@wiki`` partition from its page and embed the sections
    when the semantic tier is up. Returns the number of sections embedded."""
    nodes, texts = _wiki_section_nodes(repo_id, page, filename)
    part = _wiki_partition(repo_id)
    store.clear_repo(part)
    if not nodes:
        return 0
    store.upsert_nodes(part, nodes)
    write_shard(store_dir, GraphShard(repo=part, head_commit=head or "wiki",
                                      nodes=nodes, edges=[]))
    if embedder is not None and vs is not None:
        return _embed_documents(vs, embedder, part, nodes, texts, batch_size)
    return 0


def cmd_wiki(args) -> int:
    """Generate provenance-stamped wiki pages from the graph, gated by an LLM council."""
    from .llm import build_llm
    from .wiki.council import LENSES, council_gate
    from .wiki.generate import generate_page, render_prompt, repo_brief

    store, store_dir = _open_store(args)
    embedder = vs = None
    try:
        cfg = load_kb_config(getattr(args, "config", None))
        apply_llm_overrides(cfg, provider=getattr(args, "llm", None),
                            model=getattr(args, "llm_model", None))
        llm = build_llm(cfg.llm)
        if llm is None:
            log("LLM tier disabled — pass --llm builtin|ollama|openai "
                "(or set [llm] enabled = true in kb.toml)")
            return 0
        targets = _connect_targets(args, store)
        if not targets:
            wanted = [a for a in (getattr(args, "args", None) or []) if a]
            if wanted:
                log(f"No indexed repo matches {', '.join(wanted)} — check the exact repo id "
                    "(see `contextlake status`).")
                return 1
            log("No indexed repos (run index first, or pass --workspace/--source)")
            return 0
        wiki_dir = store_dir / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        log(f"Generating wiki for {len(targets)} repo(s) with {llm.name} "
            f"(council of {len(LENSES)})")
        # Semantic tier (optional): accepted pages also embed into the @wiki
        # partition so NL search can land on the prose (labeled advisory).
        if cfg.embeddings.enabled:
            from .embeddings import build_embedder
            from .embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is not None:
                vs = build_vector_store(store_dir / "embeddings.sqlite",
                                        backend=cfg.embeddings.vector_backend,
                                        chunk_size=cfg.embeddings.vector_chunk_size)
        force = getattr(args, "force", False)
        written = rejected = skipped = failed = 0
        for repo_id, _ in targets:
            brief = repo_brief(store_dir, repo_id)
            if brief is None:
                continue
            wiki_file = wiki_dir / (repo_id.replace("/", "__") + ".md")
            # freshness: skip the (expensive) LLM call when an existing page was
            # already generated from this repo's current head commit.
            if not force and wiki_file.exists() and brief.get("head"):
                prev = wiki_file.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"at commit `([^`]+)`", prev)
                if m and m.group(1) == brief["head"]:
                    # Backfill: a page written before the @wiki partition existed
                    # gets its sections stored/embedded without a new LLM call.
                    if store.get_node(f"{_wiki_partition(repo_id)}:0") is None:
                        _store_wiki_partition(store, store_dir, repo_id, prev,
                                              wiki_file.name, brief.get("head"),
                                              embedder, vs, cfg.embeddings.batch_size)
                    skipped += 1
                    continue
            try:
                page = generate_page(llm, store_dir, repo_id)
                gate = council_gate(llm, page, render_prompt(brief),
                                    accept_score=cfg.llm.accept_score)
            except Exception as e:  # noqa: BLE001 - one repo must not abort the run
                log(f"  {style.fail(repo_id)}: {e}")
                failed += 1
                continue
            if gate["accepted"]:
                wiki_file.write_text(page, encoding="utf-8")
                _store_wiki_partition(store, store_dir, repo_id, page,
                                      wiki_file.name, brief.get("head"),
                                      embedder, vs, cfg.embeddings.batch_size)
                written += 1
                log(f"  {style.ok(repo_id)}: written (score {gate['score']})")
            else:
                rejected += 1
                log(f"  {style.warn(repo_id)}: rejected by council (score {gate['score']})")
                for issue in gate["issues"][:5]:
                    log(f"      - {issue}")
        log(f"{style.ok()} Wiki: {written} written, {rejected} rejected, "
            f"{skipped} unchanged (skipped) → {wiki_dir}  (--force to regenerate)")
        # Honest exit: failures with nothing written and nothing council-rejected
        # means the LLM was effectively unreachable for the whole run -> not success.
        if failed and not written and not rejected:
            log(style.warn(f"Wiki generation failed for all {failed} repo(s) — none written"))
            return 1
        return 0
    finally:
        if vs is not None:
            vs.close()
        store.close()


def cmd_steer(args) -> int:
    """Generate per-tool steering files (AGENTS.md, CLAUDE.md, .windsurfrules,
    .kiro/steering, .mcp.json) that point local AI tools at the knowledge graph."""
    import json as _json

    from .steer.generate import (
        BEGIN,
        END,
        LEGACY_BEGIN,
        LEGACY_END,
        LEGACY_MARKER,
        MARKER,
        mcp_server_entry,
        render_agents_md,
        render_claude_md,
        render_kiro_steering,
        render_windsurfrules,
        workspace_facts,
    )
    from .steer.skills import skill_files

    def _upsert_block(path: Path, body: str) -> None:
        """Write our managed block, preserving any existing user content.

        Fresh file -> write the block. Already has our block -> refresh just that
        block. User's own file -> append our block at the end (nothing of theirs
        is removed)."""
        block = f"{BEGIN}\n{body.strip()}\n{END}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(block + "\n", encoding="utf-8")
            return
        existing = path.read_text(encoding="utf-8", errors="ignore")
        # Refresh an existing managed block in place — current markers first, then
        # the legacy gitlab-sync ones, so a pre-rename block is replaced (not duplicated).
        for begin, end in ((BEGIN, END), (LEGACY_BEGIN, LEGACY_END)):
            if begin in existing and end in existing:
                b, e = existing.index(begin), existing.index(end) + len(end)
                path.write_text(existing[:b] + block + existing[e:], encoding="utf-8")
                return
        # No managed block yet — append ours, keeping all the user's own content.
        glue = "\n\n" if not existing.endswith("\n") else (
            "" if existing.endswith("\n\n") else "\n")
        path.write_text(existing + glue + block + "\n", encoding="utf-8")

    store, store_dir = _open_store(args)
    try:
        out = Path(getattr(args, "out", None) or getattr(args, "workspace", None) or ".").resolve()
        config_path = getattr(args, "config", None)
        force = getattr(args, "force", False)
        facts = workspace_facts(store, store_dir)
        out.mkdir(parents=True, exist_ok=True)

        # Markdown steering: enhanced in place (managed block), never overwriting
        # a user's own content.
        steering = {
            "AGENTS.md": render_agents_md(facts, config_path=config_path),
            "CLAUDE.md": render_claude_md(config_path),
            ".windsurfrules": render_windsurfrules(facts, config_path=config_path),
            ".kiro/steering/workspace.md": render_kiro_steering(facts, config_path=config_path),
        }
        for rel, content in steering.items():
            _upsert_block(out / rel, content)

        # Skills/workflows are whole files in named dirs: write ours, refresh ours,
        # but never clobber a same-named file the user already wrote (unless --force).
        skills = skill_files()
        skipped = 0
        for rel, content in skills.items():
            p = out / rel
            if p.exists() and not force:
                _content = p.read_text(errors="ignore")
                if MARKER not in _content and LEGACY_MARKER not in _content:
                    skipped += 1
                    continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        log(f"  {style.ok(f'steering enhanced + {len(skills) // 2} skills')} "
            + (f"({skipped} foreign skill file(s) kept)" if skipped else "written"))

        # .mcp.json: merge our server entry, preserving any others the user has.
        mcp = out / ".mcp.json"
        data = {}
        if mcp.exists():
            try:
                data = _json.loads(mcp.read_text())
            except _json.JSONDecodeError:
                data = {}
        data.setdefault("mcpServers", {})["contextlake-kb"] = mcp_server_entry(config_path)
        mcp.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
        log(f"  {style.ok('.mcp.json')} (contextlake-kb MCP server, other servers kept)")

        log(f"{style.ok()} Steering written to {out} (existing files enhanced, not replaced)")
        return 0
    finally:
        store.close()


def lint_result(store, store_dir) -> dict:
    """Graph health as pure data: stale repos (HEAD moved past the index) + dangling
    edges (an endpoint whose node is missing). Shared by ``cmd_lint`` and the
    ``graph_health`` MCP tool. Reads local git HEADs — offline.
    """
    from .store.shards import read_shard

    repos = store.list_repos()
    stale_repos: list[str] = []
    dangling: list[dict] = []
    checked = 0
    node_cache: dict[str, bool] = {}

    def _exists(node_id: str) -> bool:
        if node_id not in node_cache:
            node_cache[node_id] = store.get_node(node_id) is not None
        return node_cache[node_id]

    for r in repos:
        head = _git_head(Path(r.path)) if r.path else None
        if needs_reindex(store, r.id, head):
            stale_repos.append(r.id)
        shard = read_shard(store_dir, r.id)
        if shard is None:
            continue
        for e in shard.edges:
            checked += 1
            if not _exists(e.src) or not _exists(e.dst):
                dangling.append({"repo": r.id, "src": e.src,
                                 "relation": e.relation, "dst": e.dst})
    return {"repos": len(repos), "checked": checked,
            "stale": len(stale_repos), "dangling": len(dangling),
            "stale_repos": stale_repos, "dangling_sample": dangling[:20]}


def cmd_lint(args) -> int:
    """Graph-health checks: stale repos (HEAD moved) and dangling edges."""
    store, store_dir = _open_store(args)
    try:
        if not store.list_repos():
            log("Nothing indexed yet — run index first.")
            return 0
        res = lint_result(store, store_dir)
        for rid in res["stale_repos"]:
            log(f"  stale: {rid} (HEAD moved or never finished — re-run index)")
        for d in res["dangling_sample"]:
            log(f"  dangling: {d['repo']}: {d['src']} -{d['relation']}-> {d['dst']}")
        if res["dangling"] > 20:
            log(f"  … and {res['dangling'] - 20} more dangling edge(s)")
        clean = res["dangling"] == 0 and res["stale"] == 0
        glyph = style.ok() if clean else style.warn()
        log(f"{glyph} Lint: {res['repos']} repos, {res['checked']} edges checked — "
            f"{res['dangling']} dangling, {res['stale']} stale")
        return 0 if clean else 1
    finally:
        store.close()


def _print_hit(n) -> None:
    loc = f"{n.file}:{n.line_start}" if n.file and n.line_start else (n.file or "?")
    log(f"  {style.cyan(n.repo)} · {loc} · {n.kind} · {style.bold(n.name)}")


def _query_as_of(args, commit: str) -> int:
    """Search a repo's snapshot at an indexed commit (bi-temporal 'as of')."""
    from .store.shards import read_shard_at

    repo = getattr(args, "repo", None)
    if not repo:
        log("--as-of requires --repo (history is per-repo)")
        return 2
    text = " ".join(getattr(args, "args", []) or []).strip().lower()
    store_dir = load_kb_config(getattr(args, "config", None)).store_path
    shard = read_shard_at(store_dir, repo, commit)
    if shard is None:
        log(f"No indexed snapshot of {repo!r} at commit {commit!r}")
        return 1
    kind = getattr(args, "kind", None)
    hits = [
        n for n in shard.nodes
        if (text in n.name.lower()
            or (n.qualified_name and text in n.qualified_name.lower()))
        and (kind is None or n.kind == kind)
    ][:getattr(args, "limit", None) or 20]
    if not hits:
        log(f"No matches for {text!r} in {repo} as of {commit}")
        return 0
    for n in hits:
        _print_hit(n)
    return 0


def cmd_query(args) -> int:
    text = " ".join(getattr(args, "args", []) or []).strip()
    if not text:
        log("usage: contextlake query \"<text>\" [--kind K] [--repo R] [--limit N] [--as-of C]")
        return 2
    as_of = getattr(args, "as_of", None)
    if as_of:
        return _query_as_of(args, as_of)
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
            _print_hit(n)
        return 0
    finally:
        store.close()


def cmd_owners(args) -> int:
    """Likely owners / SMEs for a repo (or sub-path), from git commit history."""
    from .ownership import compute_owners

    target = (getattr(args, "args", []) or [None])[0]
    if not target:
        log('usage: contextlake owners <repo|path> [--path SUBDIR] [--limit N]')
        return 2
    subpath = getattr(args, "path", None)
    limit = getattr(args, "limit", None) or 10

    # Resolve a working dir: a directory on disk, else a repo id looked up in the store.
    if Path(target).is_dir():
        repo_path = Path(target).resolve()
        label = repo_path.name           # resolve first so "." yields the dir name
    else:
        store, _ = _open_store(args)
        try:
            repo = store.get_repo(target)
        finally:
            store.close()
        if not repo or not repo.path:
            log(f"Unknown repo {target!r} — index it first, or pass a path on disk")
            return 1
        repo_path, label = Path(repo.path), repo.id

    owners = compute_owners(repo_path, subpath, limit=limit)
    scope = f"{label}:{subpath}" if subpath else label
    if not owners:
        log(f"No commit history found for {scope}")
        return 0
    log(f"Owners / SMEs for {style.cyan(scope)} (recency-weighted):")
    for i, o in enumerate(owners, 1):
        log(f"  {i}. {o.name}  —  {o.commits} commit(s), {o.lines} line(s), "
            f"last {o.last_active}, {o.share * 100:.0f}%")
    return 0


def cmd_impact(args) -> int:
    """What could break if a node changes — reverse blast radius over the graph."""
    from .impact import blast_radius, resolve_target

    target = (getattr(args, "args", []) or [None])[0]
    if not target:
        log('usage: contextlake impact <node-id-or-symbol> [--repo R] [--hops N] [--limit N]')
        return 2
    hops = getattr(args, "hops", None) or 3
    limit = getattr(args, "limit", None) or 100
    repo = getattr(args, "repo", None)
    store, _ = _open_store(args)
    try:
        node, candidates = resolve_target(store, target, repo=repo)
        if node is None and candidates:        # same name in several repos — disambiguate
            by_repo: dict = {}
            for c in candidates:
                by_repo.setdefault(c.repo, c)
            log(f"{style.cyan(target)} is ambiguous — defined in {len(by_repo)} repos. "
                f"Narrow it with --repo:")
            for r, c in list(by_repo.items())[:10]:
                log(f"  --repo {r}   ({c.kind} {c.name})")
            return 1
        if node is None:
            log(f"No node matches {target!r} — index first, or try `query`")
            return 1
        hits, truncated = blast_radius(store, node.id, hops=hops, limit=limit)
        head = f"Impact of changing {style.cyan(node.name)} ({node.id})"
        if not hits:
            log(f"{head}: nothing depends on it within {hops} hop(s)")
            return 0
        log(f"{head}: {len(hits)} affected node(s) within {hops} hop(s)"
            + (style.dim(" (truncated)") if truncated else ""))
        for h in hits:
            log(f"  h{h.hop}  {h.repo}:{h.name}  ({h.kind}, via {h.via}, "
                f"{h.confidence.lower()})")
        return 0
    finally:
        store.close()


def _embed_documents(vs, embedder, repo_id, nodes, texts, batch_size) -> int:
    """Embed document *bodies* into the vector store (real RAG over content), keyed by
    node id — separate from code-node embedding, which embeds the name/signature."""
    from .embeddings.store import guard_store_identity
    written = 0
    for i in range(0, len(nodes), batch_size):
        bn, bt = nodes[i:i + batch_size], texts[i:i + batch_size]
        try:
            vecs = embedder.embed(bt)
        except Exception:  # noqa: BLE001 - an unreachable embedder ends the embed phase
            break
        if i == 0 and vecs and vecs[0]:
            identity = (getattr(embedder, "identity", None)
                        or getattr(embedder, "name", "embedder"))
            guard_store_identity(vs, identity, len(vecs[0]))
        vs.upsert((n.id, repo_id, v) for n, v in zip(bn, vecs))
        written += len(bn)
    return written


def cmd_ingest(args) -> int:
    """Aggregate external documents (built-in + plugin sources) into the graph and,
    when embeddings are enabled, the semantic store."""
    from .model import Node
    from .sources import build_source, discover_sources
    from .store.shards import GraphShard, write_shard

    store, store_dir = _open_store(args)
    try:
        cfg = load_kb_config(getattr(args, "config", None))
        registry = discover_sources()
        # Zero-config fast path: `ingest --path DIR [--source-type files]`.
        jobs = []  # (name, type, options)
        cli_path = getattr(args, "path", None)
        if cli_path:
            jobs.append(("cli", getattr(args, "source_type", None) or "files",
                         {"path": cli_path}))
        else:
            for s in cfg.sources:
                if s.type in registry:
                    jobs.append((s.name or s.type, s.type,
                                 getattr(s, "model_extra", None) or {}))
        if not jobs:
            log('No document sources. Try: contextlake ingest --path ./docs  '
                '(or add [[sources]] type="files" path="…" to kb.toml). '
                f"Available source types: {', '.join(sorted(registry))}")
            return 0

        embedder = vs = None
        if cfg.embeddings.enabled:
            from .embeddings import build_embedder
            from .embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is not None:
                vs = build_vector_store(store_dir / "embeddings.sqlite",
                                        backend=cfg.embeddings.vector_backend,
                                        chunk_size=cfg.embeddings.vector_chunk_size)

        total = embedded = failed = 0
        try:
            for name, stype, options in jobs:
                src = build_source(stype, **options)
                if src is None:
                    log(f"  {name}: unknown source type {stype!r} — skipping")
                    failed += 1
                    continue
                repo_id = f"@ingest:{name}"
                nodes, texts = [], []
                try:
                    for doc in src.iter_documents():
                        nodes.append(Node(id=f"{repo_id}:{doc.id}", repo=repo_id,
                                          kind="document", name=doc.title,
                                          file=(doc.uri or None),
                                          attrs={**doc.attrs, "source": stype}))
                        texts.append(doc.text)
                except Exception as e:  # noqa: BLE001 - one source must not abort the run
                    log(f"  {name}: source failed — {e}")
                    failed += 1
                    continue
                if not nodes:
                    log(f"  {name}: no documents")
                    continue
                store.clear_repo(repo_id)
                store.upsert_nodes(repo_id, nodes)
                write_shard(store_dir, GraphShard(repo=repo_id, head_commit="ingest",
                                                  nodes=nodes, edges=[]))
                total += len(nodes)
                msg = f"  {name}: {len(nodes)} document(s)"
                if embedder is not None and vs is not None:
                    n = _embed_documents(vs, embedder, repo_id, nodes, texts,
                                         cfg.embeddings.batch_size)
                    embedded += n
                    msg += f", {n} embedded"
                log(msg)
            tail = f", {embedded} embedded into the semantic store" if embedded else ""
            log(f"Ingest complete: {total} document(s) aggregated{tail}")
            return 1 if (failed and total == 0) else 0
        finally:
            if vs is not None:
                vs.close()
    finally:
        store.close()


def cmd_serve(args) -> int:
    from .server import run_server  # imported here so `query`/`index` don't load it

    store, store_dir = _open_store(args)
    vector_store = None
    try:
        # CLI exposes "http"; the MCP SDK calls it "streamable-http".
        transport = "streamable-http" if getattr(args, "transport", None) == "http" else "stdio"
        host = getattr(args, "host", None) or "127.0.0.1"
        port = getattr(args, "port", None) or 8765

        # On stdio, stdout carries the MCP JSON-RPC stream — keep our logs off it.
        if transport == "stdio":
            from ..logging_setup import use_stderr

            use_stderr()

        # Expose semantic_search only when embeddings are enabled and a vector store exists.
        cfg = load_kb_config(getattr(args, "config", None))
        embedder = None
        from .embeddings import build_embedder

        candidate = build_embedder(cfg.embeddings)
        vec_path = store_dir / "embeddings.sqlite"
        if candidate is not None and vec_path.exists():
            from .embeddings.store import build_vector_store

            embedder = candidate
            vector_store = build_vector_store(vec_path, backend=cfg.embeddings.vector_backend)
            log(f"Semantic search enabled ({vector_store.name} store, "
                f"{vector_store.count()} vectors)")
        else:
            # Say so out loud: these two tools silently vanishing from the MCP tool
            # list otherwise reads as a broken server, not an unconfigured tier.
            why = ("no [embeddings] config" if candidate is None
                   else "no vector store yet — run: contextlake embed")
            log(style.dim(f"semantic_search / hybrid_search not registered ({why}); "
                          "graph search and every other tool work without them"))

        log(f"Serving knowledge graph over MCP ({transport})")
        run_server(store, transport=transport, host=host, port=port,
                   embedder=embedder, vector_store=vector_store)
        return 0
    finally:
        if vector_store is not None:
            vector_store.close()
        store.close()


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = style.green("✓") if ok else style.red("✗")
    print(f"  {mark} {label}" + (f" {style.dim('— ' + detail)}" if detail else ""))
    return ok


def _builtin_model_present(cache_dir, model_id: str) -> bool:
    """True if a HuggingFace-cached model dir exists under ``cache_dir/hub``.

    Filesystem-only — never imports the heavy dep or triggers a download."""
    hub = Path(cache_dir).expanduser() / "hub"
    return (hub / ("models--" + model_id.replace("/", "--"))).is_dir()


def cmd_doctor(args) -> int:
    print("contextlake knowledge layer — doctor")
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

        emb = cfg.embeddings
        if not emb.enabled:
            _check("embeddings", True, "disabled")
        else:
            vec_path = store_dir / "embeddings.sqlite"
            count, backend = 0, emb.vector_backend
            if vec_path.exists():
                from .embeddings.store import build_vector_store

                vs = build_vector_store(vec_path, backend=emb.vector_backend)
                try:
                    count, backend = vs.count(), vs.name
                finally:
                    vs.close()
            detail = f"{emb.provider} · {backend} · {count} vector(s)"
            _check("embeddings", True,
                   detail if vec_path.exists() else f"{detail} (run embed to build)")
            if emb.provider in ("builtin", "auto"):
                from .embeddings.builtin import BuiltinEmbedder

                be = BuiltinEmbedder(engine=getattr(emb, "engine", "model2vec"),
                                     model=getattr(emb, "model", None),
                                     cache_dir=getattr(emb, "cache_dir", None))
                present = _builtin_model_present(be.cache_dir, be.model_id)
                _check("  built-in embedder model", True,
                       f"{be.model_id} · "
                       f"{'downloaded' if present else 'not downloaded (run embed to fetch)'}"
                       f" · {be.cache_dir}")

            # ANN capability: can sqlite-vec actually load here? (else semantic
            # search silently uses brute-force cosine — fine small, slow at scale)
            from .embeddings.store import build_vector_store
            try:
                _vs = build_vector_store(":memory:", backend="sqlite-vec")
                _vs.close()
                ann = True
            except Exception:  # noqa: BLE001 - any import/load failure means no ANN
                ann = False
            _check("  ANN index (sqlite-vec)", ann,
                   "available — native KNN" if ann else
                   "not loadable — brute-force cosine (ok at small scale; install sqlite-vec "
                   "+ a sqlite3 that allows extensions for ANN at scale)")

        llm = cfg.llm
        if not llm.enabled:
            _check("wiki LLM", True, "disabled")
        elif llm.provider in ("builtin", "auto"):
            from .llm.builtin import BuiltinLlm

            kw = {}
            if getattr(llm, "model", None):
                kw["repo_id"] = llm.model
            if getattr(llm, "model_file", None):
                kw["filename"] = llm.model_file
            if getattr(llm, "cache_dir", None):
                kw["cache_dir"] = llm.cache_dir
            bl = BuiltinLlm(**kw)
            present = _builtin_model_present(bl.cache_dir, bl.repo_id)
            _check("wiki LLM", True,
                   f"{llm.provider} · {bl.repo_id} · "
                   f"{'downloaded' if present else 'not downloaded (run wiki to fetch)'}")
        else:
            _check("wiki LLM", True, f"{llm.provider} · {llm.model or 'default model'}")
    except Exception as e:  # noqa: BLE001 - doctor reports, never crashes
        ok &= _check("config + store", False, str(e))

    print(style.bold(style.green("OK")) if ok else style.bold(style.red("Problems found")))
    return 0 if ok else 1


def _has_seed(args) -> bool:
    return bool(getattr(args, "node", None) or getattr(args, "name", None)
                or getattr(args, "search", None)
                or " ".join(getattr(args, "args", []) or []).strip())


def cmd_graph(args) -> int:
    from . import visualize as viz

    store, store_dir = _open_store(args)
    # Generated artifacts live in a dedicated dir next to the store, never the cwd
    # or the user's home — keep generated content close to the knowledge base.
    graphs_dir = store_dir / "graphs"
    try:
        # --site: build the cross-linked offline folder and stop
        site = getattr(args, "site", None)
        if site is not None:
            out_dir = Path(site) if site else (graphs_dir / "site")
            repos_arg = getattr(args, "repos", None)
            patterns = [p.strip() for p in repos_arg.split(",") if p.strip()] if repos_arg else None
            if patterns:
                log(f"Building cross-linked graph site (repos matching {patterns})…")
            else:
                log("Building cross-linked graph site…")
            viz.build_site(store, out_dir, repos=patterns, log=log)
            log(style.ok(f"Wrote site -> {out_dir}  (open {out_dir / 'index.html'})"))
            return 0

        fmt = getattr(args, "format", None) or "html"
        # When a text format is streamed to stdout (no --output), every log line
        # would otherwise land on stdout too and corrupt the payload (e.g. a
        # truncation warning before `classDiagram`, making the Mermaid/JSON invalid).
        # Redirect logs to stderr up front, before any payload-building log fires.
        if (fmt in ("json", "dot", "mermaid", "classdiagram")
                and not getattr(args, "output", None) and not getattr(args, "serve", False)):
            from ..logging_setup import use_stderr
            use_stderr()
        max_fanout = getattr(args, "max_fanout", None) or 50
        hops = getattr(args, "hops", None) or 2
        overview = getattr(args, "overview", False)
        # The overview is a fleet inventory — default to loading every repo (so any
        # is findable); neighbourhood/repo views stay bounded at 500.
        max_nodes = getattr(args, "max_nodes", None) or (5000 if overview else 500)

        meta: dict = {}
        if overview:
            nodes, edges = viz.overview_subgraph(store, max_nodes=max_nodes, meta=meta)
            meta["mode"] = "overview"
        elif getattr(args, "repo", None) and not _has_seed(args):
            nodes, edges = viz.repo_subgraph(store, args.repo, max_nodes=max_nodes, meta=meta)
            meta.update(mode="repo", repo=args.repo)
        else:
            seeds = viz.seed_ids_from_args(store, args)
            if not seeds:
                log("usage: contextlake graph (--node ID | --name NAME | --search TEXT | "
                    "--repo R | --overview) [--hops N] [--format html|dot|mermaid|json]")
                return 2
            nodes, edges = viz.extract_subgraph(
                store, seeds, hops=hops, max_nodes=max_nodes, max_fanout=max_fanout,
                relation=getattr(args, "relation", None),
                direction=getattr(args, "direction", None) or "both", meta=meta)
            meta.update(mode="neighborhood", seed_ids=seeds, hops=hops)

        payload = viz.to_payload(nodes, edges, meta)
        cdn = getattr(args, "cdn", False)
        # cose (organic clusters) suits small neighbourhoods; for the fleet-scale
        # overview default to the instant, uniform concentric rings (hubs centred).
        layout = getattr(args, "layout", None) or ("concentric" if overview else "cose")

        if getattr(args, "serve", False):
            host = getattr(args, "host", None) or "127.0.0.1"
            port = getattr(args, "port", None) or 8765
            if overview:
                # serve the whole cross-linked site, rendering repo pages on demand
                viz.serve_site(store, host=host, port=port, max_nodes=max_nodes,
                               overview_layout=layout, max_fanout=max_fanout)
            else:
                viz.serve_graph(store, payload, host=host, port=port,
                                cdn=cdn, layout=layout, max_fanout=max_fanout)
            return 0

        if fmt == "json":
            text = viz.to_json(payload)
        elif fmt == "dot":
            text = viz.to_dot(payload)
        elif fmt == "mermaid":
            text = viz.to_mermaid(payload)
        elif fmt == "classdiagram":
            text = viz.to_class_diagram(payload)
        else:
            text = viz.to_html(payload, cdn=cdn, layout=layout)

        out = getattr(args, "output", None)
        if fmt == "html" and not out:
            # default into the dedicated graphs dir, not the cwd
            name = "overview.html" if overview else "graph.html"
            graphs_dir.mkdir(parents=True, exist_ok=True)
            out = str(graphs_dir / name)
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(text, encoding="utf-8")
            log(style.ok(f"Wrote {fmt} ({len(payload['nodes'])} nodes, "
                         f"{len(payload['edges'])} edges) -> {out}"))
            if fmt == "html" and getattr(args, "open", False):
                import webbrowser
                webbrowser.open("file://" + str(Path(out).resolve()))
        else:
            from ..logging_setup import use_stderr
            use_stderr()
            print(text)
        return 0
    finally:
        store.close()


def cmd_eval(args) -> int:
    """Score a golden-query set against the index — precision@k / recall@k / MRR."""
    from . import eval as kb_eval

    golden_path = getattr(args, "golden", None)
    if not golden_path:
        log("usage: contextlake eval --golden FILE.json [--limit K]")
        return 2
    try:
        golden = kb_eval.load_golden(golden_path)
    except (OSError, ValueError, TypeError) as e:
        log(f"Cannot load golden set {golden_path!r}: {e}")
        return 1
    k = getattr(args, "limit", None) or 10
    retr_kind = (getattr(args, "retriever", None) or "fts").lower()
    store, store_dir = _open_store(args)
    try:
        if retr_kind == "fts":
            retriever = kb_eval.make_fts_retriever(store)
        else:
            cfg = load_kb_config(getattr(args, "config", None))
            from .embeddings import build_embedder
            from .embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is None:
                log(_embed_unavailable_hint(cfg.embeddings))
                return 1
            vs = build_vector_store(store_dir / "embeddings.sqlite",
                                    backend=cfg.embeddings.vector_backend)
            factory = (kb_eval.make_semantic_retriever if retr_kind == "semantic"
                       else kb_eval.make_hybrid_retriever)
            retriever = factory(store, vs, embedder)
        result = kb_eval.evaluate(store, golden, k=k, retriever=retriever)
    finally:
        store.close()
    log(style.ok(f"Eval [{retr_kind}]: {result['n']} queries @k={k}  ·  "
                 f"P@k={result['precision@k']}  R@k={result['recall@k']}  "
                 f"MRR={result['mrr']}  hit-rate={result['hit_rate']}  ·  "
                 f"{result['est_tokens_per_query']} tok/q  "
                 f"P/1k-tok={result['precision_per_1k_tokens']}"))
    for p in result["per_query"]:
        mark = "✓" if p["hit"] else "✗"
        log(f"  {mark} {p['query'][:60]:60s} P={p['precision@k']:.2f} "
            f"R={p['recall@k']:.2f} rr={p['rr']:.2f}")
    return 0


def cmd_dashboard(args) -> int:
    """The knowledge-system dashboard — fleet / repo / relationships / impact / health
    / search UI over the local graph. ``--serve`` runs a local server (default);
    ``--site DIR`` materializes a static offline export.
    """
    from .dashboard.server import serve_dashboard
    from .dashboard.site import build_dashboard_site

    store_dir = load_kb_config(getattr(args, "config", None)).store_path
    dash_dir = store_dir / "dashboard"

    sample = getattr(args, "sample", False)
    site = getattr(args, "site", None)
    if site is not None:
        out_dir = Path(site) if site else (dash_dir / "site")
        anonymize = getattr(args, "anonymize", False)
        repos = getattr(args, "repos", None)
        group_depth = getattr(args, "group_depth", None) or 1
        src = "the bundled demo fleet" if sample else "the local store"
        log(f"Building dashboard site from {src}…")
        build_dashboard_site(store_dir, out_dir, repos=repos, anonymize=anonymize,
                             sample=sample, group_depth=group_depth)
        log(style.ok(f"Wrote dashboard -> {out_dir}  (open {out_dir / 'index.html'})"))
        return 0

    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or 8765
    if sample:
        # The advertised zero-setup preview: serve the bundled demo fleet from an
        # ephemeral store, never the user's real data.
        import shutil
        import tempfile

        from .dashboard.site import materialize_sample_store
        tmp = Path(tempfile.mkdtemp(prefix="contextlake-dash-sample-"))
        try:
            log("Serving the bundled demo fleet (fictional data, nothing local is read)…")
            serve_dashboard(materialize_sample_store(tmp), host=host, port=port,
                            open_browser=getattr(args, "open", False))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0
    serve_dashboard(store_dir, host=host, port=port,
                    open_browser=getattr(args, "open", False))
    return 0


def dispatch(command: str, args) -> int:
    return {
        "index": cmd_index, "connect": cmd_connect, "embed": cmd_embed,
        "lint": cmd_lint, "wiki": cmd_wiki, "steer": cmd_steer, "query": cmd_query,
        "serve": cmd_serve, "graph": cmd_graph, "doctor": cmd_doctor, "eval": cmd_eval,
        "owners": cmd_owners, "impact": cmd_impact, "ingest": cmd_ingest,
        "dashboard": cmd_dashboard,
    }[command](args)
