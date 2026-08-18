"""`contextlake kb query` -- full-text + as-of search over the graph."""

from __future__ import annotations

import json

from ... import style
from ...logging_setup import log
from .._util import _or_default
from ._common import (
    _open_store,
    kb_config,
)


def _print_hit(n) -> None:
    """One hit, including the qualified name when it says something the name does not.

    Search matches over name, qualified_name AND file, so a hit can be explained entirely
    by a field the text output never printed: three functions all called `hook` came back
    for `evolve` because their QUALIFIED names sit inside
    `test_hook_evolve_name_updates_auto_alias`. From the printed line the result looked
    arbitrary. `--json` carried `qualified_name` all along; this is the same fact reaching
    the humans.

    Only shown when it adds something. For a top-level symbol the qualified name is often
    just the name again, and repeating it on every line would bury the cases where it is
    the whole explanation.
    """
    loc = f"{n.file}:{n.line_start}" if n.file and n.line_start else (n.file or "?")
    line = f"  {style.cyan(n.repo)} · {loc} · {n.kind} · {style.bold(n.name)}"
    qual = (n.qualified_name or "").strip()
    if qual and qual != n.name:
        line += f" · {style.dim(qual)}"
    log(line)


def _hit_json(n) -> dict:
    return {"repo": n.repo, "file": n.file, "line": n.line_start, "kind": n.kind,
            "name": n.name, "qualified_name": n.qualified_name}


def _query_as_of(args, commit: str, *, as_json: bool = False) -> int:
    """Search a repo's snapshot at an indexed commit (bi-temporal 'as of')."""
    from ..store.shards import read_shard_at

    repo = getattr(args, "repo", None)
    if not repo:
        log("--as-of requires --repo (history is per-repo)")
        return 2
    text = " ".join(getattr(args, "args", []) or []).strip().lower()
    store_dir = kb_config(args).store_path
    shard = read_shard_at(store_dir, repo, commit)
    if shard is None:
        if as_json:
            print(json.dumps({"error": "no_snapshot", "repo": repo, "commit": commit}, indent=2))
            return 1
        log(f"No indexed snapshot of {repo!r} at commit {commit!r}")
        return 1
    kind = getattr(args, "kind", None)
    hits = [
        n for n in shard.nodes
        if (text in n.name.lower()
            or (n.qualified_name and text in n.qualified_name.lower()))
        and (kind is None or n.kind == kind)
    ][:_or_default(getattr(args, "limit", None), 20)]
    if as_json:
        print(json.dumps([_hit_json(n) for n in hits], indent=2))
        return 0
    if not hits:
        log(f"No matches for {text!r} in {repo} as of {commit}")
        return 0
    for n in hits:
        _print_hit(n)
    return 0


_QUERY_USAGE = ('contextlake kb query "<text>" [--kind K] [--repo R] [--limit N] '
               "[--as-of C] [--retriever fts|semantic|hybrid]")


def _refuse_below_floor(store, text: str, *, as_json: bool) -> bool:
    """Apply the shared relevance floor to a semantic/hybrid CLI query.

    The MCP tools have refused an unanchored query since 6.0.0 -- a
    nearest-neighbour index returns its k nearest however far away they are, and
    every one of them is a real node with a real file and line, so the answer reads
    as cited while being about nothing that was asked. `kb query` called the same
    retriever factories directly and skipped the floor, so the same store answered
    the same question two different ways depending on which surface asked.

    The CLI does not merely go quiet the way a tool result does: it names the terms
    the index has never seen, which is what makes the refusal checkable and
    retryable rather than a dead end. The exit code stays 0 -- "nothing in here is
    about that" is a valid answer to a valid question, and a script that greps the
    output should not have to special-case it.
    """
    from ..relevance import term_anchors

    unmatched, anchored = term_anchors(store, text)
    if not unmatched or anchored:
        return False
    if as_json:
        print(json.dumps([], indent=2))
    terms = ", ".join(repr(t) for t in unmatched)
    log(f"No matches for {text!r}: nothing indexed matches {terms}.")
    log("  No results are shown rather than the nearest k, which would all be "
        "real nodes and none of them about this query. Index the repo that "
        "should answer it, or retry with a term the graph knows.")
    return True


def _semantic_results(args, store, text, limit):
    """Node ids from the semantic/hybrid retriever, or ``None`` to fall back to fts
    (embedder/vector store unavailable, or the retriever itself failed) -- reuses
    the exact retriever factories `contextlake kb eval --retriever` scores, so `query`
    never has its own copy of the embedding/rerank logic."""
    from .. import eval as kb_eval
    from ..embeddings import build_embedder
    from ..embeddings.store import build_vector_store

    retr_kind = getattr(args, "retriever", None)
    cfg = kb_config(args)
    embedder = build_embedder(cfg.embeddings)
    if embedder is None:
        from .embed import _embed_unavailable_hint
        log(_embed_unavailable_hint(cfg.embeddings) + " -- showing fts results instead.")
        return None
    try:
        from ..embeddings.store import unpopulated_reason
        vs = build_vector_store(cfg.store_path / "embeddings.sqlite",
                                backend=cfg.embeddings.vector_backend)
        # An empty vector table answers every query with `[]`, which the caller cannot
        # tell apart from a real miss -- and `init` leaves embeddings ENABLED while no
        # vectors exist, so that is the state of every workspace on its first run. The
        # embedder being built successfully says the model loaded, not that anything
        # was ever embedded; those are separate facts and only the second one is the
        # one this search needs.
        reason = unpopulated_reason(vs, getattr(args, "repo", None))
        if reason is not None:
            log(f"Semantic search cannot answer: {reason}. Showing fts results instead.")
            return None
        factory = (kb_eval.make_semantic_retriever if retr_kind == "semantic"
                   else kb_eval.make_hybrid_retriever)
        retriever = factory(store, vs, embedder)
        return retriever(text, limit, repo=getattr(args, "repo", None))
    except Exception as e:  # noqa: BLE001 - any embedder/vector-store failure degrades to fts
        log(f"Semantic search unavailable ({e}) -- showing fts results instead.")
        return None


def cmd_query(args) -> int:
    text = " ".join(getattr(args, "args", []) or []).strip()
    as_json = getattr(args, "json", False)
    if as_json:
        from ...logging_setup import use_stderr
        use_stderr()
    if not text:
        if as_json:
            print(json.dumps({"error": "missing_argument", "usage": _QUERY_USAGE}, indent=2))
        else:
            log(f"usage: {_QUERY_USAGE}")
        return 2
    as_of = getattr(args, "as_of", None)
    if as_of:
        return _query_as_of(args, as_of, as_json=as_json)
    store, store_dir = _open_store(args)
    try:
        limit = _or_default(getattr(args, "limit", None), 20)
        retr_kind = (getattr(args, "retriever", None) or "fts").lower()
        results = None
        if retr_kind in ("semantic", "hybrid"):
            # Before the retriever, not inside it: the factories are the ones
            # `kb eval --retriever` scores, and a floor applied there would silently
            # move the numbers that whole measurement campaign is built on.
            #
            # Gated on embeddings being enabled, which is exactly the condition
            # under which the MCP server registers `semantic_search`/`hybrid_search`
            # at all -- so the gate makes the two surfaces agree more precisely,
            # not less. Without embeddings this falls through to fts, which has a
            # real notion of "no match" and needs no floor; refusing there would
            # print a reason ("rather than the nearest k") naming a search that was
            # never going to run, in a change whose whole point is saying true
            # things. The answer is empty on both paths either way.
            if (kb_config(args).embeddings.enabled
                    and _refuse_below_floor(store, text, as_json=as_json)):
                return 0
            ids = _semantic_results(args, store, text, limit)
            if ids is not None:
                kind = getattr(args, "kind", None)
                results = [n for nid in ids if (n := store.get_node(nid)) is not None
                          and (kind is None or n.kind == kind)]
        if results is None:
            results = store.search(
                text, kind=getattr(args, "kind", None), repo=getattr(args, "repo", None),
                limit=limit,
            )
        if as_json:
            print(json.dumps([_hit_json(n) for n in results], indent=2))
            return 0
        if not results:
            log(f"No matches for {text!r}")
            # "nothing indexed" and "indexed, no such symbol" are different answers and
            # they printed identically. A query also CREATES the store file, so a user
            # who forgot to index, or whose --local config points somewhere else than
            # the shell they are querying from, got a confident false negative with a
            # freshly made empty database behind it. Naming the path is the load-bearing
            # half: the usual cause is the right command against the wrong store.
            if store.stats().nodes == 0:
                log(style.warn(f"This store is empty (no repositories indexed): {store_dir}"))
                log("  Run `contextlake kb index <path>` first, or `contextlake kb doctor` "
                    "to see which store this config resolves to.")
                return 0
            # A multi-word phrase reads as a natural-language question; a plain fts
            # query is keyword search, so point at --retriever semantic instead of a
            # bare dead-end. Already-semantic/hybrid runs skip this (they tried it).
            if retr_kind == "fts" and len(text.split()) > 1:
                log("  (query is keyword search; for natural-language search try "
                    "`--retriever semantic` after running `contextlake kb embed`)")
            return 0
        for n in results:
            _print_hit(n)
        return 0
    finally:
        store.close()
