"""`contextlake connect` -- reconcile issue/doc references found in a repo."""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from .._util import _or_default
from ._common import (
    _connect_targets,
    _guard_store,
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
    # Both the agent UI's ticket URL and a Help Center article URL. The instance
    # subdomain is required by the pattern, not optional: a bare `zendesk.com/...`
    # names no instance and the connector would have nothing to key a node on.
    "zendesk.com": r"https://[\w-]+\.zendesk\.com/[^\s)\]>.,;'\"}]+",
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


class _StagedVectors:
    """Holds one repo's vector writes until every source for that repo has answered.

    The enrichers embed their own nodes while they run, and a pass that is going to
    replace a partition has to sweep that partition's old vectors before those writes
    land (sweeping afterwards would delete them). But whether the partition may be
    replaced at all is only known once every source has answered: if one of them was
    unreachable, the previous partition must stay, and the sweep is the one step that
    cannot be undone. So the enrichers are handed this object in place of the vector
    store. ``upsert`` is held back; ``flush`` then sweeps and writes in the order the
    old code produced, and ``discard`` drops what was held. Everything else passes
    through to the real store.
    """

    def __init__(self, real) -> None:
        self._real = real
        self._held: list = []

    def upsert(self, items) -> int:
        rows = list(items)
        self._held.extend(rows)
        return len(rows)

    def discard(self) -> None:
        self._held = []

    def flush(self, part: str) -> None:
        rows, self._held = self._held, []
        self._real.clear_repo(part)
        if rows:
            self._real.upsert(rows)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _build_enrichers(sources, store, *, embedder=None, vector_store=None, dropped=None):
    """Turn configured sources into callables ``fn(repo_id, keys, links, symbol_keys)``
    that return ``(nodes, edges)``. Atlassian sources discover their sites up front
    and are the only ones that use ``symbol_keys`` (per-symbol ticket attribution);
    every other source ignores it. ``store`` is threaded through to GitLab, Figma,
    and Slack sources, which need it to match diff-touched files / frame names /
    message-mentioned symbols to existing code nodes. ``embedder``/``vector_store``
    are threaded through to those same three sources so the connector nodes they
    build become embeddable, same as ``enrich``'s own documents.

    A source that cannot be built (Atlassian site discovery failed, or it sees no
    site) is dropped from the result. When ``dropped`` is a list, the name of each such
    source is appended to it, so the caller can tell "this source was never asked"
    from "this source has nothing to say".
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
        enrich_repo_zendesk,
        zendesk_hosts,
    )

    enrichers, names = [], []
    for s in sources:
        if s.type == "atlassian":
            conn = build_atlassian(s)
            try:
                sites = conn.discover_sites()
            except Exception as e:  # noqa: BLE001 - a dead source must not abort the run
                log(f"  source {s.name!r}: site discovery failed — {e}")
                if dropped is not None:
                    dropped.append(s.name)
                continue
            if not sites:
                # Reached and authorized, yet no site is visible. Say what to check,
                # and say that the source is being dropped -- a bare "0 site(s)"
                # followed by silence reads as a report, not as a skipped source.
                log(f"  source {s.name!r} (atlassian): authorized, but no sites are "
                    f"visible to this token (scopes: {conn.scopes!r})")
                log("    skipping it. If those scopes lack read:jira-work or "
                    "read:page:confluence, clear the cached grant and re-authorize, "
                    "or set `scopes` on the source.")
                if dropped is not None:
                    dropped.append(s.name)
                continue
            log(f"  source {s.name!r} (atlassian): {len(sites)} site(s) reachable")
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
        elif s.type == "zendesk":
            hosts = zendesk_hosts(s)
            log(f"  source {s.name!r} (zendesk): ready, no network needed")
            enrichers.append(
                lambda repo_id, keys, links, symbol_keys, h=hosts, st=store,
                       e=embedder, v=vector_store:
                enrich_repo_zendesk(h, repo_id, st, links=links, embedder=e,
                                    vector_store=v)
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
    if not _guard_store(store_dir, "connect"):
        store.close()
        return 1
    try:
        cfg = kb_config(args)
        sources = [s for s in cfg.sources
                   if s.type in ("atlassian", "figma", "gitlab", "slack", "zendesk")
                   and s.enabled]
        if not sources:
            log('No connector sources configured '
                '(add [[sources]] type="atlassian"/"figma"/"gitlab"/"slack"/"zendesk")')
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

        # The enrichers get a stand-in that holds their vector writes back, so a repo
        # whose partition ends up kept (a source was unreachable) never had its old
        # vectors swept. See `_StagedVectors` and the flush in `_one_repo`.
        staged = _StagedVectors(vector_store) if vector_store is not None else None
        dropped: list[str] = []
        enrichers, names = _build_enrichers(
            sources, store, embedder=embedder, vector_store=staged, dropped=dropped)
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
            repo_failed = 0
            # Repos whose previous partition was left alone because a source was
            # unreachable (it raised, or its call was written off as unavailable).
            kept = 0

            def _one_repo(repo_id: str, path: str) -> int:
                """Enrich one repo; returns the number of edges stored for it."""
                nonlocal attempts, src_failed, kept

                keys = extract_issue_keys(path, branch_key) if branch_key else []
                links = scrape_links(path, link_patterns) if link_patterns else []
                symbol_keys = _symbol_keys_for(store_dir, repo_id, path, branch_key)
                if not keys and not links and not symbol_keys and not has_gitlab:
                    return 0  # GitLab sources fetch by repo, so don't skip when one exists
                part = connect_partition(repo_id)
                if dropped:
                    # A source that could not be built is as unavailable as one that
                    # failed mid-run, and its links are part of this partition. Writing
                    # the partition from the sources that remain would delete them.
                    kept += 1
                    log(f"  {repo_id}: kept the previous links, source(s) "
                        f"{', '.join(map(repr, dropped))} unavailable", inline=True)
                    return 0
                # Connector nodes are embedded by the enrichers themselves (see
                # orchestrate._embed_connector_nodes), so the stale-vector sweep has
                # to happen BEFORE their writes land. Clearing alongside the graph's
                # own `store.clear_repo(part)` below would delete the vectors this pass
                # just wrote. The enrichers write through `staged`, which holds those
                # writes back: the sweep itself waits until every source has answered
                # (it cannot be undone, and a repo with an unreachable source keeps
                # its old vectors), then runs before the held writes are applied.
                if staged is not None:
                    staged.discard()
                merged_nodes, merged_edges = {}, {}
                failed_sources: list[str] = []
                for name, enrich in zip(names, enrichers, strict=True):
                    attempts += 1
                    # Connector methods do not raise: a dead source returns nothing and
                    # logs why through `note_unavailable`, which bumps this counter. An
                    # empty answer and an unreachable source look the same otherwise.
                    degraded_mark = degraded_calls()
                    try:
                        nodes, edges = enrich(repo_id, keys, links, symbol_keys)
                    except Exception as e:  # noqa: BLE001 - one source/repo must not abort the run
                        log(f"  {repo_id}: source {name!r} failed ({e})", inline=True)
                        src_failed += 1
                        failed_sources.append(name)
                        continue
                    if degraded_calls() > degraded_mark:
                        failed_sources.append(name)
                    for n in nodes:
                        merged_nodes[n.id] = n
                    for ed in edges:
                        merged_edges[(ed.src, ed.dst, ed.relation)] = ed
                if failed_sources:
                    # One partition holds every source's links, so a source that did not
                    # answer leaves the whole previous partition standing: a partial
                    # rewrite would drop that source's share of it and read as "nothing
                    # to link". An EMPTY answer is not this case, and clears below.
                    if staged is not None:
                        staged.discard()
                    kept += 1
                    log(f"  {repo_id}: kept the previous links, source(s) "
                        f"{', '.join(map(repr, failed_sources))} unavailable", inline=True)
                    return 0
                if staged is not None:
                    staged.flush(part)
                store.clear_repo(part)
                store.upsert_nodes(part, list(merged_nodes.values()))
                store.upsert_edges(part, list(merged_edges.values()))
                if merged_edges:
                    log(f"  {repo_id}: {len(merged_edges)} link(s)", inline=True)
                return len(merged_edges)

            for repo_id, path in targets:
                # One repository must not take the fleet down with it. The
                # per-source guard inside `_one_repo` only covers the enricher
                # calls; everything around them -- reading the repo's branches and
                # commit subjects, blaming its files, writing its partition -- ran
                # unguarded, so a single unreadable repository aborted the whole
                # run before the other nineteen were reached. That is exactly how
                # one commit carrying a non-UTF-8 byte killed a 20-repo fleet.
                #
                # A repo that throws before the flush in `_one_repo` keeps both its
                # vectors and its graph edges. One that throws after it has had its
                # vectors rewritten and keeps its previous graph edges until the next
                # run. Incomplete either way, and the exit code below says so
                # rather than leaving it silent.
                try:
                    total_edges += _one_repo(repo_id, path)
                except Exception as e:  # noqa: BLE001 - reported per repo, never aborts the run
                    repo_failed += 1
                    log(f"  {style.fail(repo_id)}: {e}", inline=True)

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
                "ok" if not (degraded or repo_failed or kept) else "warn",
                f"Connect complete: {total_edges} external link(s) stored"))
            if kept:
                log(style.warn(
                    f"{kept} of {len(targets)} repo(s) kept their previous links because a "
                    "source was unavailable (reasons logged above)."))
            if repo_failed:
                log(style.warn(
                    f"{repo_failed} of {len(targets)} repo(s) failed and were skipped; "
                    "the rest were enriched. Re-run to retry them, or narrow with "
                    "`contextlake kb connect <repo-id>`."))
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
            # A source that could not be reached is a failure of the run even when
            # other repos got links: those links hide the outage, and the repos that
            # kept their previous partition are not the graph this run was asked to
            # build. Same verdict, and the same escape hatch, as `kb ingest` gives a
            # failed source. The flag is a PRE-command global (`contextlake
            # --exit-zero-on-partial kb connect`).
            if (kept or degraded) and not repo_failed:
                if getattr(args, "exit_zero_on_partial", False):
                    log(style.dim("  Exiting 0 (--exit-zero-on-partial)."))
                    return 0
                log("  For a scheduled run that should tolerate this:")
                log("    contextlake --exit-zero-on-partial kb connect")
                return 1
            # A skipped repository is missing knowledge, so the run is not clean --
            # same verdict `kb index` gives a workspace where one repo failed to
            # parse, and for the same reason: the graph an agent will cite from is
            # not the one this command was asked to build.
            return 1 if repo_failed else 0

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
