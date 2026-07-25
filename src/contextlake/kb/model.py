"""Knowledge-graph data model.

The graph has three primary entities — :class:`Repo`, :class:`Node`, and
:class:`Edge`. Every :class:`Edge` carries :class:`Provenance` and a
:class:`Confidence`: the anti-hallucination contract is structural, not advisory,
so a fact in the graph always knows where it came from and how sure it is.

``kind`` (node) and ``relation`` (edge) are intentionally open vocabularies, the
way Graphify treats them — new parsers/connectors can introduce new kinds and
relations without a schema migration.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field

SHARED_REPO = "(shared)"
"""Pseudo-repo for node kinds no single repo owns (``module``/``endpoint``/``topic`` —
an imported package, an HTTP route, an event topic can be produced/consumed by many
repos at once). One of a small family of such sentinels, each naming *why* a node has
no single owner: ``"(packages)"`` for manifest-declared package identities
(``kb/manifest.py``), ``"(external)"`` for connector-fetched nodes with no repo at all
(Figma designs, Atlassian issues). The store dedupes these nodes to one row per store
(their id doesn't encode a repo), so a single ``repo_id`` column can't hold "owned by
many" — per-repo attribution for a shared node lives on its *edges* instead (each
``imports``/``exposes``/``calls_http``/``publishes_event``/``consumes_event`` edge is
attributed to the file that produced it, which always has exactly one real owning
repo — see ``arch/resolve.py``, which already reads attribution this way, never from
the shared node's own ``repo``). Using a stable sentinel instead of the last repo
indexed also means ``clear_repo`` on any one repo can never delete a node other repos
still reference."""


class Confidence(str, Enum):
    """How much to trust an edge."""

    EXTRACTED = "EXTRACTED"  # derived directly from source (AST/manifest) — ground truth
    INFERRED = "INFERRED"  # deduced (e.g. a second-pass call graph, LLM suggestion)
    AMBIGUOUS = "AMBIGUOUS"  # uncertain; flagged for a human/agent to verify


class Provenance(BaseModel):
    """Where a fact came from. Required on every edge."""

    source_file: str
    source_line: int | None = None
    verified_at: date


class Node(BaseModel):
    """A vertex: a repo, file, module, symbol, package, concept, …"""

    id: str
    repo: str
    kind: str  # open vocabulary: file | module | class | function | symbol | package | concept | …
    name: str
    qualified_name: str | None = None
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    lang: str | None = None
    attrs: dict = Field(default_factory=dict)


class Edge(BaseModel):
    """A directed relationship between two nodes, with provenance + confidence."""

    src: str
    dst: str
    relation: str  # open vocabulary: calls | imports | depends_on | references | …
    confidence: Confidence
    provenance: Provenance
    context: str | None = None  # e.g. call | import | field | parameter_type | return_type
    weight: float = 1.0


class Repo(BaseModel):
    """An indexed repository (host-agnostic)."""

    id: str  # canonical, from the repo's remote (see repo_identity.py); e.g.
             # "gitlab.example.com/team/service-api", stable across workspace roots
    path: str  # absolute local path of the clone
    host: str | None = None
    default_branch: str | None = None
    head_commit: str | None = None
