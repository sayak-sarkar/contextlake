"""Golden-query evaluation harness for retrieval quality.

Define a small labelled set of ``query -> expected nodes`` and run it through any
retriever (FTS search, semantic, hybrid) to get **precision@k / recall@k / MRR**,
so a retrieval change (embed-bodies, reranking, the future ``ask`` router) is
*falsifiable* rather than vibes — a regression shows up as a number dropping.

Stdlib-only; the golden set is plain JSON:

    {"queries": [
      {"query": "ForecastService", "expected": ["demo_app_forecastservice"]},
      {"query": "load readings", "expected": ["load_readings"], "match": "name", "kind": "function"}
    ]}

``match`` is ``"id"`` (default — compare against node ids) or ``"name"`` (compare
against node names, handy when ids are path-derived and unstable).

Check a new entry scores before you add it. A query that matches nothing still
counts, so it drags the whole set down without saying why. Measured against
``examples/fixtures/sample-graph.json``: both queries above score P@k=1.00, while
``forecast service`` scores 0.00 against the same ``ForecastService`` node.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store.base import Store

# A retriever maps (query, k, kind, repo) -> a ranked list of node ids. It closes
# over whatever it needs (store, vector store, embedder) — built by the make_*
# factories below — so semantic/hybrid retrievers are scorable, not just FTS.
Retriever = Callable[..., list]


MATCH_MODES = ("id", "name")


@dataclass
class GoldenQuery:
    query: str
    expected: list  # node ids, or names when match == "name"
    kind: str | None = None
    repo: str | None = None
    match: str = "id"  # "id" | "name"

    def __post_init__(self):
        """Reject a golden entry this cannot score, instead of scoring it zero.

        Every field is checked, because every field has a malformed value that reaches the
        scorer, produces no error, and comes back as a confident number:

        - an unrecognised `match` mode falls through every comparison and misses;
        - `expected` as a bare string is iterable, so the scorer compares against its
          individual CHARACTERS and naturally matches nothing;
        - a non-string inside `expected` (`0`, `null`, a nested list) can never equal a
          retrieved node id or name, so it is a guaranteed miss dressed as a measurement;
        - an empty `query` gives the full-text layer no terms, and no terms retrieves
          nothing -- scored as a retrieval failure rather than an incomplete entry;
        - a falsy `kind` or `repo` (`false`, `[]`, `{}`) is dropped by the store's
          `if kind:` filter test, so the query silently runs UNFILTERED and can score a
          hit the filter it appears to carry would have excluded.

        A wrong number is the worst possible answer here. `kb eval --json` exists to gate CI
        on a metric, so a typo in the golden file reads as a real retrieval result and either
        blocks a release or passes one, with the numbers looking measured rather than
        meaningless.
        """
        if self.match not in MATCH_MODES:
            raise ValueError(
                f"query {self.query!r}: match={self.match!r} is not one of "
                f"{', '.join(MATCH_MODES)}")
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError(
                f"query must be a non-empty string, not {self.query!r}; an empty query "
                f"retrieves nothing, which would be scored as a retrieval failure rather "
                f"than as the incomplete golden entry it is.")
        if isinstance(self.expected, str):
            raise ValueError(
                f"query {self.query!r}: expected must be a LIST of "
                f"{'names' if self.match == 'name' else 'node ids'}, not a string. "
                f'Write ["{self.expected}"].')
        if not isinstance(self.expected, (list, tuple)) or not self.expected:
            raise ValueError(
                f"query {self.query!r}: expected must be a non-empty list; a query with "
                f"nothing to find cannot be scored, and counting it as a miss would drag "
                f"the whole set's metrics down for a file that is simply incomplete.")
        for item in self.expected:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"query {self.query!r}: every entry in expected must be a non-empty "
                    f"{'name' if self.match == 'name' else 'node id'} string, but one is "
                    f"{item!r}. It can never equal a retrieved value, so it would score a "
                    f"guaranteed miss that reads as a real one.")
        for field, value in (("kind", self.kind), ("repo", self.repo)):
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"query {self.query!r}: {field}={value!r} is neither omitted nor a "
                    f"non-empty string. The store drops a falsy filter, so this query "
                    f"would run UNFILTERED while appearing to carry a {field} filter.")


def load_golden(path) -> list[GoldenQuery]:
    """Parse a golden set, raising rather than returning something unscorable.

    ``{"queries": [{"query": ..., "expected": [...], "match": "id"|"name"}]}``. A missing
    `queries` key, an empty one, a bad `match`, or a string `expected` all raise here so the
    CLI reports `bad_golden_set` -- which is a different thing from a real score of zero, and
    the caller must be able to tell them apart.

    An EMPTY `queries` is rejected for that same reason and it is the easy one to miss: it
    parses, it iterates, and it scores. What comes back is `n: 0` with every metric at 0.0
    and exit 0, which is indistinguishable from a set that ran and retrieved nothing.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "queries" not in data:
        raise ValueError(
            'golden set must be an object with a "queries" list: '
            '{"queries": [{"query": "...", "expected": ["..."], "match": "name"}]}')
    queries = data["queries"]
    if not isinstance(queries, list) or not queries:
        raise ValueError(
            f'"queries" must be a non-empty list, not {queries!r}. Scoring an empty set '
            f"reports n=0 with every metric at 0.0, which cannot be told apart from a real "
            f"run that retrieved nothing.")
    for i, q in enumerate(queries):
        if not isinstance(q, dict):
            raise ValueError(
                f"queries[{i}] must be an object with query/expected keys, not {q!r}")
    return [GoldenQuery(**q) for q in queries]


def make_fts_retriever(store: Store) -> Retriever:
    """The always-available baseline: the store's full-text search."""
    def _retrieve(query, k, kind=None, repo=None):
        return [n.id for n in store.search(query, kind=kind, repo=repo, limit=k)]
    return _retrieve


def make_semantic_retriever(store, vector_store, embedder) -> Retriever:
    """Pure embedding search (kind is ignored — vectors aren't kind-filtered)."""
    def _retrieve(query, k, kind=None, repo=None):
        vec = embedder.embed([query])[0]
        return [nid for nid, _score in vector_store.search(vec, k=k, repo=repo)]
    return _retrieve


def make_hybrid_retriever(store, vector_store, embedder) -> Retriever:
    """Semantic seed + Personalized-PageRank rerank over the graph."""
    from .embeddings.hybrid import hybrid_search

    def _retrieve(query, k, kind=None, repo=None):
        ranked = hybrid_search(store, vector_store, embedder, query, k=k, repo=repo)
        return [nid for nid, _score in ranked]
    return _retrieve


def _est_tokens(node) -> int:
    """Rough token cost (~chars/4) of surfacing one node to an agent's context."""
    parts = [node.kind or "", node.qualified_name or node.name or "", node.file or ""]
    sig = getattr(node, "signature", None)  # present once embed-bodies lands
    if sig:
        parts.append(sig)
    return max(1, len(" ".join(parts)) // 4)


def _result_tokens(store, ids: list) -> int:
    """Estimated token cost of returning these node ids — the price of the answer."""
    total = 0
    for nid in ids:
        n = store.get_node(nid)
        if n:
            total += _est_tokens(n)
    return total


# --- citation verification ------------------------------------------------------
#
# Retrieval metrics score whether the right node came back. They say nothing about
# whether the `file:line` that node carries still points at the symbol -- and that
# citation is the whole promise: an agent is told to go read it. A stale or wrong
# citation is worse than a miss, because it looks like an answer.
#
# Three outcomes, never two. "verified" and "broken" are the interesting pair, but a
# node whose repository has no local checkout is **unverifiable**, and folding that
# into either one is how a harness starts lying: counted as broken it invents defects
# on a machine that simply lacks the mirror, counted as ok it reports a clean bill of
# health for checks that never ran.

_CITE_WINDOW = 2
"""Lines either side of ``line_start`` that may carry the name.

The reasoning is that a definition's recorded start line is the start of the
*construct*, so the name can sit a line or two in: a C++ return type on its own line, a
template header, a decorator above a Python ``def``.

Measured rather than asserted, and the measurement is unflattering: on a 3,000-node
sample of a large legacy C/C++ tree, going from a window of 0 to 2 reclaimed **one**
node. Widening to 5 reclaimed nothing more. So this is cheap insurance against a real
idiom, not a fix for a widespread problem -- and it is deliberately kept at 2, because
the cost of a window is that it can bless a citation pointing a couple of lines off
target."""

_CASE_INSENSITIVE_LANGS = frozenset({"sql"})
"""Languages whose identifiers do not distinguish case, so neither may the check.

This is not leniency, it is the language. ``kb/sql.py``'s ``_norm_name`` casefolds
every DDL object name on purpose (SQL identifiers are case-insensitive, and foreign-key
attribution matches on the normalised form), so a table declared ``CREATE TABLE Foo``
is stored as ``foo``. Measured on a real tree, a case-sensitive check called **12 of 13**
table citations broken while every one of them pointed at exactly the right line: a
checker's first job is not to invent defects."""

_CITE_MAX_LINES = 200_000
"""Give up rather than read an unbounded file into memory. Generated headers and
god-files in legacy trees really do run past this, and a checker that can hang on one
node is a checker nobody runs."""


@dataclass
class CitationCheck:
    node_id: str
    status: str   # "verified" | "broken" | "unverifiable"
    reason: str   # "" when verified
    cite: str     # "path:line" as the answer would present it, or ""


def _read_upto(path: Path, upto: int) -> list[str] | None:
    """The file's lines up to ``upto`` (1-based), or None if it cannot be read.

    Reads lazily and stops: verifying line 40 of a 40,000-line file should cost 40
    lines, not the file."""
    from itertools import islice
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return list(islice(fh, min(upto, _CITE_MAX_LINES)))
    except (OSError, ValueError):
        return None


def verify_citations(store: Store, node_ids: list, *,
                     window: int = _CITE_WINDOW) -> list[CitationCheck]:
    """Check that each node's ``file:line`` really contains its name, on disk, now.

    Per-node reasons, all of them things that have actually gone wrong in this project's
    history rather than hypotheticals: ``node_missing`` (a retriever returned an id the
    store does not hold), ``no_citation`` (a symbol node with no file or no line -- an
    answer that cannot be cited at all), ``file_missing`` (the graph outlived the file),
    ``line_out_of_range`` (the file shrank under a stale index) and ``name_absent`` (the
    line exists but the symbol is not on it, the failure mode a size or line check
    cannot see).

    Two reasons are *unverifiable* rather than broken. ``checkout_missing`` is the one
    that matters: the recorded clone path is not on this machine, which ``doctor``
    already reports as an ``unreadable`` repo. It has to be separated from
    ``file_missing``, because when the whole checkout is gone every file under it is
    gone too -- calling that a stale graph would manufacture a defect per result.
    ``file_unreadable`` (a directory where a file is recorded, an unreadable mode) is
    the same kind of "could not look".
    """
    out: list[CitationCheck] = []
    roots: dict[str, Path | None] = {}
    file_cache: dict[tuple[str, int], list[str] | None] = {}
    for nid in node_ids:
        node = store.get_node(nid)
        if node is None:
            out.append(CitationCheck(nid, "broken", "node_missing", ""))
            continue
        if node.repo not in roots:
            r = store.get_repo(node.repo)
            p = Path(r.path) if r and r.path else None
            roots[node.repo] = p if (p and p.is_dir()) else None
        root = roots[node.repo]
        cite = f"{node.file}:{node.line_start}" if node.file else ""
        if not node.file:
            out.append(CitationCheck(nid, "broken", "no_citation", ""))
            continue
        if root is None:
            out.append(CitationCheck(nid, "unverifiable", "checkout_missing", cite))
            continue
        path = root / node.file
        if not path.is_file():
            out.append(CitationCheck(nid, "broken", "file_missing", cite))
            continue
        # A file node's citation IS the path; there is no name to find on a line.
        if node.kind == "file" or not node.line_start:
            status, reason = (("verified", "") if node.kind == "file"
                              else ("broken", "no_citation"))
            out.append(CitationCheck(nid, status, reason, cite))
            continue
        want = node.line_start + window
        key = (str(path), want)
        if key not in file_cache:
            file_cache[key] = _read_upto(path, want)
        lines = file_cache[key]
        if lines is None:
            out.append(CitationCheck(nid, "unverifiable", "file_unreadable", cite))
            continue
        if node.line_start > len(lines):
            out.append(CitationCheck(nid, "broken", "line_out_of_range", cite))
            continue
        lo = max(0, node.line_start - 1 - window)
        hay = "".join(lines[lo:node.line_start + window])
        needle = node.name
        if (node.lang or "") in _CASE_INSENSITIVE_LANGS:
            hay, needle = hay.casefold(), needle.casefold()
        ok = needle in hay
        out.append(CitationCheck(nid, "verified" if ok else "broken",
                                 "" if ok else "name_absent", cite))
    return out


def citation_summary(checks: list[CitationCheck]) -> dict:
    """Aggregate checks, keeping the three outcomes separate.

    ``verified_rate`` is over the **verifiable** ones only, and ``unverifiable`` is
    reported beside it, so a run on a machine with no mirror reads as "nothing was
    checked" rather than as a pass or a failure."""
    reasons: dict[str, int] = {}
    for c in checks:
        if c.reason:
            reasons[c.reason] = reasons.get(c.reason, 0) + 1
    verified = sum(1 for c in checks if c.status == "verified")
    broken = sum(1 for c in checks if c.status == "broken")
    unver = sum(1 for c in checks if c.status == "unverifiable")
    checkable = verified + broken
    return {
        "checked": len(checks),
        "verified": verified,
        "broken": broken,
        "unverifiable": unver,
        "verified_rate": round(verified / checkable, 4) if checkable else None,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "broken_examples": [{"node": c.node_id, "cite": c.cite, "reason": c.reason}
                            for c in checks if c.status == "broken"][:10],
    }


def _keys(retrieved: list, gq: GoldenQuery, store: Store) -> list:
    if gq.match == "name":
        return [(n.name if (n := store.get_node(nid)) else nid) for nid in retrieved]
    return list(retrieved)


def precision_at_k(retrieved_keys: list, expected: list, k: int) -> float:
    topk = retrieved_keys[:k]
    if not topk:
        return 0.0
    exp = set(expected)
    return sum(1 for r in topk if r in exp) / len(topk)


def recall_at_k(retrieved_keys: list, expected: list, k: int) -> float:
    if not expected:
        return 0.0
    topk = set(retrieved_keys[:k])
    return sum(1 for e in set(expected) if e in topk) / len(set(expected))


def reciprocal_rank(retrieved_keys: list, expected: list) -> float:
    exp = set(expected)
    for i, r in enumerate(retrieved_keys, 1):
        if r in exp:
            return 1.0 / i
    return 0.0


def evaluate(store: Store, golden: list[GoldenQuery], *, k: int = 10,
             retriever: Retriever | None = None, verify: bool = False) -> dict:
    """Run every golden query and aggregate precision@k / recall@k / MRR — plus a
    **cost** dimension (estimated tokens to return the answer, and precision per
    1k tokens), so "route to the cheapest sufficient source" becomes measurable.

    ``retriever`` defaults to the FTS baseline (``make_fts_retriever(store)``).

    ``verify`` additionally checks every returned node's ``file:line`` against the
    checkout (see :func:`verify_citations`). It is off by default because it does
    filesystem work proportional to the results and needs the mirror present, and
    because a metric nobody can reproduce offline should not be in the headline
    numbers by default.
    """
    if retriever is None:
        retriever = make_fts_retriever(store)
    per = []
    all_checks: list[CitationCheck] = []
    for gq in golden:
        # fetch a few extra so recall isn't capped by k when expected has many ids
        retrieved = retriever(gq.query, max(k, len(gq.expected)), gq.kind, gq.repo)
        keys = _keys(retrieved, gq, store)
        rr = reciprocal_rank(keys, gq.expected)
        tokens = _result_tokens(store, retrieved[:k]) if store is not None else 0
        row = {
            "query": gq.query,
            "precision@k": precision_at_k(keys, gq.expected, k),
            "recall@k": recall_at_k(keys, gq.expected, k),
            "rr": rr,
            "hit": rr > 0,
            "est_tokens": tokens,
        }
        if verify and store is not None:
            checks = verify_citations(store, list(retrieved[:k]))
            all_checks.extend(checks)
            row["citations"] = citation_summary(checks)
        per.append(row)
    n = len(per) or 1
    mean_prec = sum(p["precision@k"] for p in per) / n
    mean_tokens = sum(p["est_tokens"] for p in per) / n
    out = {
        "k": k,
        "n": len(per),
        "precision@k": round(mean_prec, 4),
        "recall@k": round(sum(p["recall@k"] for p in per) / n, 4),
        "mrr": round(sum(p["rr"] for p in per) / n, 4),
        "hit_rate": round(sum(1 for p in per if p["hit"]) / n, 4),
        "est_tokens_per_query": round(mean_tokens, 1),
        # precision bought per 1k tokens spent — higher is a cheaper, sharper source
        "precision_per_1k_tokens": (round(mean_prec / (mean_tokens / 1000), 4)
                                    if mean_tokens else 0.0),
        "per_query": per,
    }
    if verify:
        # Deduplicated: one node cited by five queries is one citation, and counting it
        # five times would let a single popular symbol carry the whole rate.
        seen, unique = set(), []
        for c in all_checks:
            if c.node_id not in seen:
                seen.add(c.node_id)
                unique.append(c)
        out["citations"] = citation_summary(unique)
    return out
