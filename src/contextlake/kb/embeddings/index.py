"""Embed indexed graph nodes into the vector store — the semantic-search build pass.

Reads a repo's shard (the source of truth for its nodes), turns each node into a
short text, embeds in batches via the configured provider, and upserts the vectors.
The pass is per-repo and idempotent (it clears a repo's vectors before re-adding),
so it can run incrementally and be capped for very large workspaces.
"""

from __future__ import annotations

from .._util import chunks
from ..store.shards import read_shard

# Version of the node -> text mapping below. Bumping it marks every stored vector
# stale (the next embed re-runs the fleet once, intentionally), so enriching the
# text can never leave old name-only vectors silently coexisting with new semantics.
#   1: kind + name + qualified_name + file (metadata only)
#   2: + captured signature and docstring (real content -> real semantic search)
EMBED_CONTENT_VERSION = 2

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
# names + signatures + docstrings) plus HTTP endpoints and infrastructure resources.
# Deliberately EXCLUDES file nodes (a path is not a semantic query), and the cross-repo
# *shared* nodes — module / package / topic — whose ids repeat across repos, which
# otherwise get re-embedded once per referencing repo (wasted compute, an inflated
# "written" count) and dilute results with low-signal hits. Dependents/flow tools
# cover those. Low-signal HCL kinds (variable, output, data, module, local) stay out.
EMBEDDABLE_KINDS = frozenset(
    {"class", "function", "method", "interface", "struct", "enum", "endpoint",
     "resource"})


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
    vector_store.clear_repo(repo_id)
    total = 0
    for batch in chunks(nodes, max(1, batch_size)):
        vectors = embedder.embed([node_text(n) for n in batch])
        vector_store.upsert(
            (n.id, repo_id, v) for n, v in zip(batch, vectors)
        )
        total += len(batch)
    return total
