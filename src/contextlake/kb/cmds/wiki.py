"""`contextlake wiki` -- generate provenance-stamped wiki pages, gated by the LLM council."""

from __future__ import annotations

import re

from ... import style
from ...logging_setup import log
from ..config import apply_llm_overrides, load_kb_config
from ..store.shards import GraphShard, read_shard, write_shard
from ._common import (
    _connect_targets,
    _guard_store,
    _open_store,
)
from .ingest import _embed_documents


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
    from ..model import Node

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
    from ..llm import build_llm
    from ..wiki.cluster import (
        cluster_fingerprint,
        cluster_page_name,
        cross_repo_edges,
        generate_cluster_page,
        namespace_brief,
        namespaces_at_depth,
        render_cluster_prompt,
    )
    from ..wiki.council import LENSES, council_gate
    from ..wiki.generate import generate_page, render_prompt, repo_brief

    store, store_dir = _open_store(args)
    if not _guard_store(store_dir, "wiki"):
        store.close()
        return 1
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
        if llm.name == "builtin":
            # The builtin 0.5B is a weak reviewer (near-constant ~0.95 scores, mostly
            # rubber-stamping) -- still functional, but a real backend gates meaningfully.
            log("Note: the builtin model is a weak council reviewer (tends to accept "
                "almost everything). For meaningful accept/reject gating, configure a "
                "real backend: --llm anthropic|openai|ollama|cli.")
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
            from ..embeddings import build_embedder
            from ..embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is not None:
                vs = build_vector_store(store_dir / "embeddings.sqlite",
                                        backend=cfg.embeddings.vector_backend,
                                        chunk_size=cfg.embeddings.vector_chunk_size)
        force = getattr(args, "force", False)

        # Cluster (namespace) wiki: --namespace <prefix> or --namespaces --depth N.
        namespace = getattr(args, "namespace", None)
        if namespace or getattr(args, "namespaces", False):
            if namespace:
                ns_list = [namespace]
            else:
                depth = getattr(args, "depth", None) or 2
                ns_list = namespaces_at_depth([r.id for r in store.list_repos()], depth)
            if not ns_list:
                log("No namespaces to generate cluster wiki for (index some repos first)")
                return 0
            log(f"Generating cluster wiki for {len(ns_list)} namespace(s) with {llm.name} "
                f"(council of {len(LENSES)})")
            all_edges = cross_repo_edges(store)   # scan the store once, not per namespace
            written = rejected = skipped = failed = 0
            progress = style.Progress(len(ns_list), label="wiki-cluster")
            for ns in ns_list:
                brief = namespace_brief(store, store_dir, ns, edges=all_edges)
                if brief is None:
                    log(f"  {ns}: no indexed repos under this namespace, skipping", inline=True)
                    progress.advance(ns)
                    continue
                page_file = wiki_dir / cluster_page_name(ns)
                # freshness: skip when the member commit fingerprint is unchanged.
                if not force and page_file.exists():
                    prev = page_file.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r"cluster-commits: ([0-9a-f]+)", prev)
                    if m and m.group(1) == cluster_fingerprint(brief):
                        skipped += 1
                        progress.advance(ns)
                        continue
                try:
                    page = generate_cluster_page(llm, brief)
                    gate = council_gate(llm, page, render_cluster_prompt(brief),
                                        accept_score=cfg.llm.accept_score,
                                        council_size=getattr(cfg.llm, "council_size", None))
                except Exception as e:  # noqa: BLE001 - one cluster must not abort the run
                    log(f"  {style.fail(ns)}: {e}", inline=True)
                    failed += 1
                    progress.advance(ns)
                    continue
                if gate["accepted"]:
                    page_file.parent.mkdir(parents=True, exist_ok=True)   # wiki/_clusters/
                    page_file.write_text(page, encoding="utf-8")
                    # embed cluster prose into @wiki:<namespace> (labeled advisory).
                    _store_wiki_partition(store, store_dir, ns, page, page_file.name, None,
                                          embedder, vs, cfg.embeddings.batch_size)
                    written += 1
                    log(f"  {style.ok(ns)}: written (score {gate['score']})", inline=True)
                else:
                    rejected += 1
                    log(f"  {style.warn(ns)}: rejected by council "
                        f"(score {gate['score']})", inline=True)
                progress.advance(ns)
            progress.done()
            fail_tail = f", {failed} failed" if failed else ""
            glyph = style.warn() if failed else style.ok()
            log(f"{glyph} Cluster wiki: {written} written, {rejected} rejected, "
                f"{skipped} unchanged (skipped){fail_tail} → {wiki_dir}  "
                f"(--force to regenerate)")
            if failed:
                log("  See the log above for which namespaces failed. Re-run to retry.")
            # An explicit --namespace that matched no repos is a user error, not success.
            if namespace and not (written or rejected or skipped):
                return 1
            if failed and not written and not rejected:
                return 1
            return 0

        written = rejected = skipped = failed = 0
        progress = style.Progress(len(targets), label="wiki")
        for repo_id, _ in targets:
            # Freshness check first, off the cheap shard-only head_commit --
            # repo_brief(..., store=store) below also runs setup_signals'
            # live-checkout scan, which a skipped (unchanged) repo shouldn't pay for.
            shard = read_shard(store_dir, repo_id)
            if shard is None:
                progress.advance(repo_id)
                continue
            wiki_file = wiki_dir / (repo_id.replace("/", "__") + ".md")
            head = shard.head_commit
            if not force and wiki_file.exists() and head:
                prev = wiki_file.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"at commit `([^`]+)`", prev)
                if m and m.group(1) == head:
                    # Backfill: a page written before the @wiki partition existed
                    # gets its sections stored/embedded without a new LLM call.
                    if store.get_node(f"{_wiki_partition(repo_id)}:0") is None:
                        _store_wiki_partition(store, store_dir, repo_id, prev,
                                              wiki_file.name, head,
                                              embedder, vs, cfg.embeddings.batch_size)
                    skipped += 1
                    progress.advance(repo_id)
                    continue
            brief = repo_brief(store_dir, repo_id, store=store)
            if brief is None:
                progress.advance(repo_id)
                continue
            try:
                page = generate_page(llm, store_dir, repo_id, store=store)
                gate = council_gate(llm, page, render_prompt(brief),
                                    accept_score=cfg.llm.accept_score,
                                    council_size=getattr(cfg.llm, "council_size", None))
            except Exception as e:  # noqa: BLE001 - one repo must not abort the run
                log(f"  {style.fail(repo_id)}: {e}", inline=True)
                failed += 1
                progress.advance(repo_id)
                continue
            if gate["accepted"]:
                wiki_file.write_text(page, encoding="utf-8")
                _store_wiki_partition(store, store_dir, repo_id, page,
                                      wiki_file.name, brief.get("head"),
                                      embedder, vs, cfg.embeddings.batch_size)
                written += 1
                log(f"  {style.ok(repo_id)}: written (score {gate['score']})", inline=True)
            else:
                rejected += 1
                log(f"  {style.warn(repo_id)}: rejected by council "
                    f"(score {gate['score']})", inline=True)
                for issue in gate["issues"][:5]:
                    log(f"      - {issue}")
            progress.advance(repo_id)
        progress.done()
        fail_tail = f", {failed} failed" if failed else ""
        glyph = style.warn() if failed else style.ok()
        log(f"{glyph} Wiki: {written} written, {rejected} rejected, "
            f"{skipped} unchanged (skipped){fail_tail} → {wiki_dir}  (--force to regenerate)")
        # Honest exit: failures with nothing written and nothing council-rejected
        # means the LLM was effectively unreachable for the whole run -> not success.
        if failed and not written and not rejected:
            log(style.warn(f"Wiki generation failed for all {failed} repo(s) — none written"))
            return 1
        if failed:
            log("  See the log above for which repos failed. Re-run to retry.")
        return 0
    finally:
        if vs is not None:
            vs.close()
        store.close()

