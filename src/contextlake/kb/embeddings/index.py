"""Embed indexed graph nodes into the vector store — the semantic-search build pass.

Reads a repo's shard (the source of truth for its nodes), turns each node into a
short text, embeds in batches via the configured provider, and upserts the vectors.
The pass is per-repo and idempotent (it clears a repo's vectors before re-adding),
so it can run incrementally and be capped for very large workspaces.
"""

from __future__ import annotations

from .._util import chunks
from ..kinds import KIND_REGISTRY
from ..store.shards import read_shard

# Version of everything a stored vector depends on. Bumping it marks every stored
# vector stale (the next embed re-runs the fleet once, intentionally).
#
# TWO things make a stored vector stale, and only the first used to be covered:
#
#  1. The node -> text mapping below. Enriching the text must never leave old
#     name-only vectors silently coexisting with new semantics.
#  2. **The shape of node ids.** A vector row is keyed by node id, so if the id
#     scheme changes, every stored key stops matching a real node. The retrieval
#     tools then drop the unresolvable hits and return a shorter, plausible,
#     non-empty answer; `doctor` still reports a healthy row count; and the
#     half-migrated case is worst of all, because the surviving hits are silently
#     biased toward whichever repos happened to be re-embedded. This version did
#     not cover ids, which is precisely why that failure was invisible.
#
# **So: any change to how node ids are built MUST bump this.** It is the only signal
# that reaches `kb embed`'s incremental path, and re-embedding is the only repair.
#
#  3. **Which kinds are embeddable.** A THIRD case, and the one this version documents.
#     Widening `EMBEDDABLE_KINDS` does not make an existing vector wrong -- it makes the
#     STORE INCOMPLETE, which is the same defect wearing different clothes. Nothing else
#     would notice: the incremental skip is keyed on (head commit, parser version), and
#     neither moves when the embeddable set does, so a user who had already embedded
#     would keep a store that silently holds no vectors for the new kinds and a
#     `doctor` that reports a healthy row count.
#
#   1: kind + name + qualified_name + file (metadata only)
#   2: + captured signature and docstring (real content -> real semantic search)
#   3: node ids became file- and line-independent (a readable slug plus a digest), so
#      every stored KEY from 1 and 2 names a node that no longer exists. The text
#      mapping itself did not change; the bump is about the keys.
#   4: five symbol kinds became embeddable (field, macro, typedef, enum_constant,
#      global_variable). Measured before the change: their recall was EXACTLY ZERO --
#      tens of thousands of symbols no semantic query could reach. See
#      `testing/d8-embedding-measurement.md` for the cost side, which is real and
#      bounded: -5.25pp existing-kind recall@10 for +180% vectors.
#   5: an ingested DOCUMENT is stored as several chunk vectors instead of one vector over
#      its whole text, and the stored key gained a chunk suffix (`store.chunk_key`). Both
#      halves of the staleness rule above apply at once: the text a vector is built from
#      changed, AND every stored key changed shape. A store left at 4 holds one averaged
#      vector per document under a key nothing writes any more.
#
#      This is the first bump made on a measurement rather than a judgement. On 29 real
#      documents with 53 position-selected queries: hit rate 71.7% -> 94.3%, MRR
#      45.2% -> 80.4%, +15 tokens per query, 13 queries fixed and 1 lost. The controls, and
#      what the measurement does NOT establish, are in `docs/semantic-search.md`.
#
#      Note what this bump does and does not do for documents. Only `kb embed` and the
#      freshness report read this number; the DOCUMENT path never consults it, so bumping
#      it marks an old store stale but does not itself re-embed a document. `kb ingest`
#      rewrites document vectors, and sweeps the partition first so the old whole-document
#      vector cannot linger next to the new chunks.
EMBED_CONTENT_VERSION = 5

# Docstrings are captured up to 1000 chars; embed a tighter slice so one verbose
# docstring can't drown the identifying tokens (name/signature) in the vector.
_DOC_EMBED_CHARS = 400


def node_text(node) -> str:
    """The text representation of a node used for embedding."""
    parts = [node.kind, node.name]
    if node.qualified_name and node.qualified_name != node.name:
        parts.append(node.qualified_name)
    if node.file:
        parts.append(node.file)
    attrs = getattr(node, "attrs", None) or {}
    if attrs.get("signature"):
        parts.append(str(attrs["signature"]))
    if attrs.get("doc"):
        parts.append(str(attrs["doc"])[:_DOC_EMBED_CHARS])
    return " ".join(p for p in parts if p)


# Kinds worth a semantic vector: code definitions (unique per repo, carrying real
# names + signatures + docstrings), data objects (tables and views), plus HTTP endpoints
# and infrastructure resources. Projected from the registry (kb/kinds.py) rather than
# retyped here — every exclusion now carries its reason in the registry's
# `why_not_embeddable`, which a test requires to be non-empty. That prose used to live in
# this comment and covered nine of the excluded kinds; it was silent on `config_key` and
# `test`, so nobody could tell a considered exclusion from an oversight.
#
# Membership is a load-bearing, sequenced decision, NOT a refactoring detail: the set feeds
# the per-kind embedding budget floors, so widening it evicts vectors that already exist
# and re-embedding is the only repair. tests/kb/test_kind_registry_parity.py pins the exact
# membership for that reason.
EMBEDDABLE_KINDS = frozenset(k for k, s in KIND_REGISTRY.items() if s.embeddable)


def embed_repo(store_dir, vector_store, embedder, repo_id, *,
               batch_size: int = 64, limit: int | None = None, kinds=None) -> int:
    """Embed a repo's semantically-meaningful nodes into ``vector_store``.

    ``kinds`` defaults to :data:`EMBEDDABLE_KINDS` (definitions + endpoints); pass an
    explicit set to override. Returns the number embedded."""
    shard = read_shard(store_dir, repo_id)
    if shard is None:
        return 0
    allowed = EMBEDDABLE_KINDS if kinds is None else kinds
    nodes = [n for n in shard.nodes if n.kind in allowed]
    if limit is not None:
        nodes = nodes[:limit]
    if not nodes and shard.nodes:
        # Nothing here is ours to embed, so nothing here is ours to delete. The
        # clear below is a replace-in-place for what this call is about to write,
        # and running it on a shard whose kinds we never embed destroys vectors
        # some other writer owns. `connect`, `enrich` and `ingest` each embed
        # their own nodes at write time, and none of those kinds (`document`,
        # `design`, `file`, `repo`) is in EMBEDDABLE_KINDS, so a single pass over
        # such a partition emptied it and reported "0 written" as if that were
        # the correct answer.
        #
        # The guard is `shard.nodes` rather than an id or prefix test on purpose:
        # it asks "did this shard have content we skipped", which is the actual
        # question, and does not couple this function to the naming of partitions.
        # An empty shard still falls through and clears, which is right: a repo
        # that lost all its nodes should lose its vectors.
        return 0
    vector_store.clear_repo(repo_id)
    total = 0
    for batch in chunks(nodes, max(1, batch_size)):
        vectors = embedder.embed([node_text(n) for n in batch])
        vector_store.upsert(
            (n.id, repo_id, v) for n, v in zip(batch, vectors, strict=True)
        )
        total += len(batch)
    return total
