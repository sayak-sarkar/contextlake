#!/usr/bin/env python3
"""Enrich the docs search index with build-time semantic intelligence.

Runs AFTER build_docs.py, under an environment where contextlake's knowledge layer is
installed (the deploy venv). It embeds every index entry with contextlake's OWN embedder
(dogfooding), then precomputes each entry's nearest semantic neighbours across other pages.
The result ships as plain JSON: no model runs in the browser, the site stays offline, and
the palette can rank + suggest semantically. If the embedder is unavailable (e.g. a lean CI
without the kb extra) this is a no-op and the lexical index is kept as-is.
"""
import json
import math
import pathlib

IDX = pathlib.Path(__file__).resolve().parent.parent / "search-index.json"


def main() -> int:
    try:
        from contextlake.kb.config import load_kb_config
        from contextlake.kb.embeddings import build_embedder
        emb = build_embedder(load_kb_config(None).embeddings)
        if emb is None:
            raise RuntimeError("no embedder configured")
    except Exception as e:  # noqa: BLE001 - any failure -> graceful skip
        print(f"  [gen_search_index] embeddings unavailable ({e}); keeping lexical index")
        return 0

    data = json.loads(IDX.read_text(encoding="utf-8"))

    vecs = emb.embed([f"{e['title']}. {e['text']}" for e in data])

    def unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    nv = [unit(v) for v in vecs]
    base = [e["url"].split("#", 1)[0] for e in data]
    for i, e in enumerate(data):
        sims = sorted(
            (
                (sum(a * b for a, b in zip(nv[i], nv[j])), j)
                for j in range(len(data))
                if j != i and base[j] != base[i]  # skip self + same-page sections
            ),
            reverse=True,
        )
        e["related"] = [data[j]["url"] for _, j in sims[:3]]

    IDX.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"  [gen_search_index] enriched {len(data)} entries with semantic neighbours")
    return 0


if __name__ == "__main__":
    sys.exit(main())
