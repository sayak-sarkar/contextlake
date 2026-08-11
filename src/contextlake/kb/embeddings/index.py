"""Embed indexed graph nodes into the vector store — the semantic-search build pass.

Reads a repo's shard (the source of truth for its nodes), turns each node into a
short text, embeds in batches via the configured provider, and upserts the vectors.
The pass is per-repo and idempotent (it clears a repo's vectors before re-adding),
so it can run incrementally and be capped for very large workspaces.
"""

from __future__ import annotations

from .._util import chunks
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
#   1: kind + name + qualified_name + file (metadata only)
#   2: + captured signature and docstring (real content -> real semantic search)
#   3: node ids became file- and line-independent (a readable slug plus a digest), so
#      every stored KEY from 1 and 2 names a node that no longer exists. The text
#      mapping itself did not change; the bump is about the keys.
EMBED_CONTENT_VERSION = 3

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
# and infrastructure resources. Deliberately EXCLUDES file nodes (a path is not a semantic
# query), and the cross-repo *shared* nodes — module / package / topic — whose ids repeat
# across repos, which otherwise get re-embedded once per referencing repo (wasted compute,
# an inflated "written" count) and dilute results with low-signal hits. Dependents/flow
# tools cover those. Low-signal HCL kinds (variable, output, data, module, local) and
# SQL procedures (low signal without a signature) stay out. `adr` (kb/adr.py) carries
# its full body under the `doc` attr, same as a docstring, so node_text() below already
# picks it up with no extra wiring.
EMBEDDABLE_KINDS = frozenset(
    {"class", "function", "method", "interface", "struct", "enum", "endpoint",
     "route", "resource", "table", "view", "adr"})


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
