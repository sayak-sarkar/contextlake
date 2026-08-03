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

PACKAGES_REPO = "(packages)"
"""Sentinel for manifest-declared package identities (``kb/manifest.py``) -- see
:data:`SHARED_REPO` for the family this belongs to."""

EXTERNAL_REPO = "(external)"
"""Sentinel for connector-fetched nodes with no repo at all (Figma designs, Atlassian
issues, GitLab entities) -- see :data:`SHARED_REPO` for the family this belongs to."""

SYSTEM_REPO = "(system)"
"""Sentinel for a runtime dependency outside the indexed fleet: an HTTP/event call
target no ``exposes``/``consumes_event`` edge from any indexed repo ever resolves to
(``kb/arch/resolve.py``). Deliberately distinct from :data:`EXTERNAL_REPO` -- that one
is connector-*fetched* content (a Figma design, a Jira issue); this one is discovered
purely from the fleet's own outbound calls, with no connector involved and no guarantee
the target is a real third party rather than simply an unindexed internal service (see
``kb/c4.py``'s C1 layer, which renders these unclassified for exactly that reason)."""


def is_sentinel_repo(repo_id: str) -> bool:
    """True for a pseudo-repo id (the :data:`SHARED_REPO` family) -- never a real
    repo, so must never be treated as one in a fleet-wide repo listing, a per-repo
    view, or the dashboard breadcrumb. The ``(``-prefix is the shared contract every
    sentinel follows, checked here and independently in ``dashboard.js``."""
    return repo_id.startswith("(")


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


EXTERNAL_LINK_RELATIONS = frozenset({
    "tracked_by",        # Atlassian: an issue tracking this repo
    "documented_by",     # Atlassian: a Confluence page describing it
    "designed_in",       # Figma: a design file/frame for it
    "has_merge_request", # GitLab: an open merge request on it
    "has_issue",         # GitLab: an open issue on it
    "touched_by",        # GitLab: a merge request whose diff touches its code
    "discussed_in",      # Slack: a channel whose history mentions its symbols
    "referenced_in",     # Slack: a channel linked from its docs
})
"""The ``repo -> external`` relations that count as *cross-links to external
knowledge*, as opposed to the code-to-code relations parsing produces.

Deliberately one shared constant rather than a literal per consumer: there are two
independent front doors onto this same surface -- the MCP ``get_repo_links`` tool
(``kb/server.py``) and the dashboard's Links panel (``kb/dashboard/data.py``'s
``_links_for``) -- and a connector that adds a relation should light up both or
neither. They were hand-duplicated once and silently drifted, so the Slack and
GitLab-diff relations reached the graph but neither door showed them.

Not a closed vocabulary check: ``Edge.relation`` stays open (see this module's
docstring). This is only the subset those two views group and render."""


class Edge(BaseModel):
    """A directed relationship between two nodes, with provenance + confidence."""

    src: str
    dst: str
    relation: str  # open vocabulary: calls | imports | depends_on | references | …
    confidence: Confidence
    provenance: Provenance
    context: str | None = None  # e.g. call | import | field | parameter_type | return_type
    weight: float = 1.0
    attrs: dict = Field(default_factory=dict)


class Repo(BaseModel):
    """An indexed repository (host-agnostic)."""

    id: str  # canonical, from the repo's remote (see repo_identity.py); e.g.
             # "gitlab.example.com/team/service-api", stable across workspace roots
    path: str  # absolute local path of the clone
    host: str | None = None
    default_branch: str | None = None
    head_commit: str | None = None
