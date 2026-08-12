"""`contextlake kb eval` -- run the golden-query regression suite."""

from __future__ import annotations

import json

from ... import style
from ...logging_setup import log
from .._util import _or_default
from ._common import (
    _open_store,
    kb_config,
)
from .embed import _embed_unavailable_hint


def cmd_eval(args) -> int:
    """Score a golden-query set against the index — precision@k / recall@k / MRR."""
    from .. import eval as kb_eval

    as_json = getattr(args, "json", False)
    if as_json:
        # stdout is reserved for the JSON payload (a CI job pipes it into a
        # threshold check) -- route the human-readable log lines to stderr,
        # same convention as `kb owners --json` / `kb impact --json`.
        from ...logging_setup import use_stderr
        use_stderr()

    golden_path = getattr(args, "golden", None)
    if not golden_path:
        usage = ("contextlake kb eval --golden FILE.json [--limit K] "
                 "[--retriever fts|semantic|hybrid] [--verify-citations] [--json]")
        if as_json:
            print(json.dumps({"error": "missing_argument", "usage": usage}, indent=2))
        else:
            log(f"usage: {usage}")
        return 2
    try:
        golden = kb_eval.load_golden(golden_path)
    except (OSError, ValueError, TypeError) as e:
        if as_json:
            print(json.dumps({"error": "bad_golden_set", "path": str(golden_path),
                              "detail": str(e)}, indent=2))
        else:
            log(f"Cannot load golden set {golden_path!r}: {e}")
        return 1
    k = _or_default(getattr(args, "limit", None), 10)
    retr_kind = (getattr(args, "retriever", None) or "fts").lower()
    store, store_dir = _open_store(args)
    vs = None  # only opened for semantic/hybrid; closed in the finally below
    try:
        if retr_kind == "fts":
            retriever = kb_eval.make_fts_retriever(store)
        else:
            cfg = kb_config(args)
            from ..embeddings import build_embedder
            from ..embeddings.store import build_vector_store
            embedder = build_embedder(cfg.embeddings)
            if embedder is None:
                hint = _embed_unavailable_hint(cfg.embeddings)
                if as_json:
                    print(json.dumps({"error": "embeddings_unavailable", "detail": hint},
                                     indent=2))
                else:
                    log(hint)
                return 1
            vs = build_vector_store(store_dir / "embeddings.sqlite",
                                    backend=cfg.embeddings.vector_backend)
            factory = (kb_eval.make_semantic_retriever if retr_kind == "semantic"
                       else kb_eval.make_hybrid_retriever)
            retriever = factory(store, vs, embedder)
        result = kb_eval.evaluate(store, golden, k=k, retriever=retriever,
                                  verify=bool(getattr(args, "verify_citations", False)))
    finally:
        if vs is not None:
            vs.close()
        store.close()

    if as_json:
        print(json.dumps({"retriever": retr_kind, **result}, indent=2))
        return 0

    log(style.ok(f"Eval [{retr_kind}]: {result['n']} queries @k={k}  ·  "
                 f"P@k={result['precision@k']}  R@k={result['recall@k']}  "
                 f"MRR={result['mrr']}  hit-rate={result['hit_rate']}  ·  "
                 f"{result['est_tokens_per_query']} tok/q  "
                 f"P/1k-tok={result['precision_per_1k_tokens']}"))
    for p in result["per_query"]:
        mark = style.ok() if p["hit"] else style.fail()
        log(f"  {mark} {p['query'][:60]:60s} P={p['precision@k']:.2f} "
            f"R={p['recall@k']:.2f} rr={p['rr']:.2f}")

    cit = result.get("citations")
    if cit:
        rate = ("n/a" if cit["verified_rate"] is None
                else f"{cit['verified_rate'] * 100:.1f}%")
        head = (f"Citations: {cit['verified']}/{cit['verified'] + cit['broken']} "
                f"verified ({rate}) of {cit['checked']} distinct nodes")
        log(style.ok(head) if cit["broken"] == 0 else style.warn(head))
        if cit["unverifiable"]:
            # Said out loud rather than folded into the rate: these were not checked.
            log(f"  {cit['unverifiable']} unverifiable (no local checkout for the repo)")
        for reason, count in cit["reasons"].items():
            log(f"  {reason}: {count}")
        for ex in cit["broken_examples"]:
            log(f"    {style.fail()} {ex['cite'] or ex['node']}  ({ex['reason']})")
    return 0
