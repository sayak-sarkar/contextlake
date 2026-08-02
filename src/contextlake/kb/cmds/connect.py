"""`contextlake connect` -- reconcile issue/doc references found in a repo."""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from ..config import load_kb_config
from ._common import (
    _connect_targets,
    _open_store,
    _watch_loop,
)

# Built-in doc-link patterns, always merged into the configured `link_scrape`
# patterns -- so Figma/Slack link discovery works with zero `[[rules]]` config,
# the same way GitLab sources are already discovered without one.
_DEFAULT_LINK_PATTERNS = {
    # Trailing chars excluded from the capture so a markdown-linked or
    # sentence-trailing URL (`[flow](https://.../Flow)`, `...archives/C1.`)
    # doesn't drag `)`/`.`/etc. into the file key or channel id. `:` stays
    # allowed -- real Figma `node-id` query values can carry it unencoded.
    "figma.com": r"https://(?:www\.)?figma.com/(?:file|design)/[^\s)\]>.,;'\"}]+",
    "slack.com": r"https://[\w-]+\.slack.com/archives/[^\s)\]>.,;'\"}]+",
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


def _build_enrichers(sources, store):
    """Turn configured sources into callables ``fn(repo_id, keys, links, symbol_keys)``
    that return ``(nodes, edges)``. Atlassian sources discover their sites up front
    and are the only ones that use ``symbol_keys`` (per-symbol ticket attribution);
    every other source ignores it. ``store`` is threaded through to GitLab and
    Figma sources, which need it to match diff-touched files / frame names to
    existing code nodes.
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
                lambda repo_id, keys, links, symbol_keys, c=conn, st=store:
                enrich_repo_figma(c, repo_id, st, links=links)
            )
            names.append(s.name)
        elif s.type == "gitlab":
            conn = build_gitlab(s)
            log(f"  source {s.name!r} (gitlab): ready")
            enrichers.append(
                lambda repo_id, keys, links, symbol_keys, c=conn, st=store:
                enrich_repo_gitlab(c, repo_id, st)
            )
            names.append(s.name)
        elif s.type == "slack":
            conn = build_slack(s)
            log(f"  source {s.name!r} (slack): ready")
            enrichers.append(
                lambda repo_id, keys, links, symbol_keys, c=conn:
                enrich_repo_slack(c, repo_id, links=links)
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
    from ..references import extract_issue_keys, scrape_links

    store, store_dir = _open_store(args)
    try:
        cfg = load_kb_config(getattr(args, "config", None))
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

        enrichers, names = _build_enrichers(sources, store)
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
                symbol_keys = _symbol_keys_for(store_dir, repo_id, path, branch_key)
                if not keys and not links and not symbol_keys and not has_gitlab:
                    continue  # GitLab sources fetch by repo, so don't skip when one exists
                merged_nodes, merged_edges = {}, {}
                for name, enrich in zip(names, enrichers):
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
                part = connect_partition(repo_id)
                store.clear_repo(part)
                store.upsert_nodes(part, list(merged_nodes.values()))
                store.upsert_edges(part, list(merged_edges.values()))
                total_edges += len(merged_edges)
                if merged_edges:
                    log(f"  {repo_id}: {len(merged_edges)} link(s)", inline=True)
            log(style.summary_line(
                "ok", f"Connect complete: {total_edges} external link(s) stored"))
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

