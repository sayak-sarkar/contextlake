"""`contextlake kb eval` -- run the golden-query regression suite."""

from __future__ import annotations

from ... import style
from ...logging_setup import log
from ..config import load_kb_config
from ._common import (
    _open_store,
)
from .embed import _embed_unavailable_hint


def cmd_eval(args) -> int:
    """Score a golden-query set against the index — precision@k / recall@k / MRR."""
    from .. import eval as kb_eval

    golden_path = getattr(args, "golden", None)
    if not golden_path:
        log("usage: contextlake kb eval --golden FILE.json [--limit K]")
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
            from ..embeddings import build_embedder
            from ..embeddings.store import build_vector_store
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
        mark = style.ok() if p["hit"] else style.fail()
        log(f"  {mark} {p['query'][:60]:60s} P={p['precision@k']:.2f} "
            f"R={p['recall@k']:.2f} rr={p['rr']:.2f}")
    return 0

