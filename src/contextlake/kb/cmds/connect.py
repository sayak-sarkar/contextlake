"""`contextlake connect` -- reconcile issue/doc references found in a repo."""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from .._util import _or_default
from ._common import (
    _connect_targets,
    _open_store,
    _watch_loop,
    kb_config,
)

# Built-in doc-link patterns, always merged into the configured `link_scrape`
# patterns -- so Figma/Slack link discovery works with zero `[[rules]]` config,
# the same way GitLab sources are already discovered without one.
_DEFAULT_LINK_PATTERNS = {
    # Trailing chars excluded from the capture so a markdown-linked or
    # sentence-trailing URL (`[flow](https://.../Flow)`, `...archives/C1.`)
    # doesn't drag `)`/`.`/etc. into the file key or channel id. `:` stays
    # allowed -- real Figma `node-id` query values can carry it unencoded.
    "figma.com": r"https://(?:www\.)?figma\.com/(?:file|design)/[^\s)\]>.,;'\"}]+",
    "slack.com": r"https://[\w-]+\.slack\.com/archives/[^\s)\]>.,;'\"}]+",
}


def _rule_patterns(rules) -> tuple[str | None, list[str]]:
    """Pull the issue-key pattern and doc-link patterns out of configured rules.

    A ``link_scrape`` rule may carry a single ``pattern`` or a ``patterns`` list
    (the latter is what the example config uses); both are accepted. The
    built-in Figma/Slack patterns (``_DEFAULT_LINK_PATTERNS``) are always
    merged in, deduplicated against whatever was explicitly configured.
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
    for builtin_pattern in _DEFAULT_LINK_PATTERNS.values():
        if builtin_pattern not in link_patterns:
            link_patterns.append(builtin_pattern)
    return branch_key, link_patterns


def _build_enrichers(sources, store, *, embedder=None, vector_store=None):
    """Turn configured sources into callables ``fn(repo_id, keys, links, symbol_keys)``
    that return ``(nodes, edges)``. Atlassian sources discover their sites up front
    and are the only ones that use ``symbol_keys`` (per-symbol ticket attribution);
    every other source ignores it. ``store`` is threaded through to GitLab, Figma,
    and Slack sources, which need it to match diff-touched files / frame names /
    message-mentioned symbols to existing code nodes. ``embedder``/``vector_store``
    are threaded through to those same three sources so the connector nodes they
    build become embeddable, same as ``enrich``'s own documents.
    Returns ``(enrichers, names)``."""
    from ..connectors.orchestrate import (
        build_atlassian,
        build_figma,
        build_gitlab,
        build_slack,
        enrich_repo,
        enrich_repo_figma,
        enrich_repo_gitlab,
        enrich_repo_slack,
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
                lambda repo_id, keys, links, symbol_keys, c=conn, st=sites:
                enrich_repo(c, st, repo_id, issue_keys=keys, links=links,
                           symbol_keys=symbol_keys)
            )
            names.append(s.name)
        elif s.type == "figma":
            conn = build_figma(s)
            log(f"  source {s.name!r} (figma): ready")
            enrichers.append(
                lambda repo_id, keys, links, symbol_keys, c=conn, st=store,
                       e=embedder, v=vector_store:
                enrich_repo_figma(c, repo_id, st, links=links, embedder=e, vector_store=v)
            )
            names.append(s.name)
        elif s.type == "gitlab":
            conn = build_gitlab(s)
            log(f"  source {s.name!r} (gitlab): ready")
            enrichers.append(
                lambda repo_id, keys, links, symbol_keys, c=conn, st=store,
                       e=embedder, v=vector_store:
                enrich_repo_gitlab(c, repo_id, st, embedder=e, vector_store=v)
            )
            names.append(s.name)
        elif s.type == "slack":
            conn = build_slack(s)
            log(f"  source {s.name!r} (slack): ready")
            enrichers.append(
                lambda repo_id, keys, links, symbol_keys, c=conn, st=store,
                       e=embedder, v=vector_store:
                enrich_repo_slack(c, repo_id, st, links=links, embedder=e, vector_store=v)
            )
            names.append(s.name)
    return enrichers, names


def _symbol_keys_for(store_dir, repo_id: str, path: str, pattern: str | None) -> dict:
    """Per-symbol candidate issue keys for one repo: docstring matches first
    (explicit, cheap), then git blame for any symbol a docstring didn't
    already resolve (implicit, one batched ``git blame`` per file). Empty
    without a configured pattern -- there's nothing to regex-match against.
    """
    if not pattern:
        return {}
    from ..connectors.symbol_refs import keys_from_blame, keys_from_docstrings
    from ..store.shards import read_shard

    shard = read_shard(store_dir, repo_id)
    if shard is None:
        return {}
    symbols = shard.nodes
    out = keys_from_docstrings(symbols, pattern)
    remaining = [n for n in symbols if n.id not in out]
    out.update(keys_from_blame(path, remaining, pattern))
    return out


def cmd_connect(args) -> int:
    from ..connectors.orchestrate import connect_partition
    from ..model import EXTERNAL_REPO
    from ..references import extract_issue_keys, scrape_links

    store, store_dir = _open_store(args)
    try:
        cfg = kb_config(args)
        sources = [s for s in cfg.sources
                   if s.type in ("atlassian", "figma", "gitlab", "slack") and s.enabled]
        if not sources:
            log('No connector sources configured '
                '(add [[sources]] type="atlassian"/"figma"/"gitlab"/"slack")')
            return 0
        has_gitlab = any(s.type == "gitlab" for s in sources)
        branch_key, link_patterns = _rule_patterns(cfg.rules)
        if not branch_key and not link_patterns and not has_gitlab:
            log('No association rules configured (add [[rules]] type="branch_key"/"link_scrape")')
            return 0

        embedder = vector_store = None
        if cfg.embeddings.enabled:
            from ..embeddings import build_embedder
            from ..embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is not None:
                vector_store = build_vector_store(
                    store_dir / "embeddings.sqlite",
                    backend=cfg.embeddings.vector_backend,
                    chunk_size=cfg.embeddings.vector_chunk_size,
                )

        enrichers, names = _build_enrichers(
            sources, store, embedder=embedder, vector_store=vector_store)
        if not enrichers:
            log("No usable connector sources; nothing to connect")
            return 1

        def _connect_once() -> int:
            from ..resilience import degraded_calls

            # Connector methods are contractually non-raising: an unreachable
            # source yields [] so one dead source cannot break the graph. That
            # makes the per-source try/except below blind to them, so the run's
            # verdict is taken from what the calls themselves reported instead.
            degraded_before = degraded_calls()
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
                symbol_keys = _symbol_keys_for(store_dir, repo_id, path, branch_key)
                if not keys and not links and not symbol_keys and not has_gitlab:
                    continue  # GitLab sources fetch by repo, so don't skip when one exists
                part = connect_partition(repo_id)
                # Connector nodes are embedded by the enrichers themselves (see
                # orchestrate._embed_connector_nodes), so the stale-vector sweep has
                # to happen BEFORE they run -- clearing alongside the graph's own
                # `store.clear_repo(part)` below would delete the vectors this pass
                # just wrote. Same guard placement as that call: only a repo whose
                # partition is about to be rewritten gets swept.
                if vector_store is not None:
                    vector_store.clear_repo(part)
                merged_nodes, merged_edges = {}, {}
                for name, enrich in zip(names, enrichers, strict=True):
                    attempts += 1
                    try:
                        nodes, edges = enrich(repo_id, keys, links, symbol_keys)
                    except Exception as e:  # noqa: BLE001 - one source/repo must not abort the run
                        log(f"  {repo_id}: source {name!r} failed ({e})", inline=True)
                        src_failed += 1
                        continue
                    for n in nodes:
                        merged_nodes[n.id] = n
                    for ed in edges:
                        merged_edges[(ed.src, ed.dst, ed.relation)] = ed
                store.clear_repo(part)
                store.upsert_nodes(part, list(merged_nodes.values()))
                store.upsert_edges(part, list(merged_edges.values()))
                total_edges += len(merged_edges)
                if merged_edges:
                    log(f"  {repo_id}: {len(merged_edges)} link(s)", inline=True)

            # `clear_repo` swept each repo's connector EDGES, which live in its
            # @connect partition; the nodes they pointed at live in `(external)`
            # and survived it. A run that fetched nothing (network down, token
            # expired) therefore left those nodes stranded with no edges in
            # either direction -- unreachable by any traversal, and invisible to
            # exactly the code questions they exist to answer. Once after the
            # loop, not once per repo: it is a store-wide sweep, and nothing
            # between iterations depends on it having run.
            store.prune_orphan_nodes(EXTERNAL_REPO)
            degraded = degraded_calls() - degraded_before
            log(style.summary_line(
                "ok" if not degraded else "warn",
                f"Connect complete: {total_edges} external link(s) stored"))
            if degraded:
                log(style.warn(
                    f"{degraded} source call(s) returned nothing because the source was "
                    "unavailable (reasons logged above); these results are incomplete"))
            # Honest exit: every source call attempted failed (e.g. an unreachable
            # connector) -> a failure, even though per-repo errors were logged.
            if attempts and src_failed == attempts:
                log(style.warn(f"All {attempts} source call(s) failed — no links stored"))
                return 1
            # Nothing stored *and* calls were written off is not an empty result,
            # it is a failed one: an expired token, a dead host and a 404 all land
            # here, and exiting 0 made them indistinguishable from a clean run
            # over a repo with no open work.
            if degraded and not total_edges:
                return 1
            return 0

        try:
            if getattr(args, "watch", False):
                interval = _or_default(getattr(args, "interval", None), 60)
                log(f"{style.cyan('watch')}: re-connecting every {interval}s (Ctrl-C to stop)")
                _watch_loop(_connect_once, interval=interval)
                return 0
            return _connect_once()
        finally:
            if vector_store is not None:
                vector_store.close()
    finally:
        store.close()
