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

    id: str  # group-relative path, e.g. "team/service-api"
    path: str  # absolute local path of the clone
    host: str | None = None
    default_branch: str | None = None
    head_commit: str | None = None
