"""Tests for the knowledge-graph data model."""

from datetime import date

import pytest
from pydantic import ValidationError

from gitlab_sync.kb.model import Confidence, Edge, Node, Provenance, Repo


def _prov():
    return Provenance(source_file="src/a.py", source_line=10, verified_at=date(2026, 6, 21))


def test_node_round_trips():
    n = Node(id="r:sym", repo="team/api", kind="function", name="handle", lang="python")
    assert Node.model_validate(n.model_dump()) == n
    assert n.attrs == {}


def test_edge_requires_provenance():
    with pytest.raises(ValidationError):
        Edge(src="a", dst="b", relation="calls", confidence=Confidence.EXTRACTED)


def test_edge_round_trips_with_confidence_and_provenance():
    e = Edge(
        src="a", dst="b", relation="calls",
        confidence=Confidence.EXTRACTED, provenance=_prov(), context="call",
    )
    dumped = e.model_dump(mode="json")
    assert dumped["confidence"] == "EXTRACTED"
    assert Edge.model_validate(dumped) == e


def test_confidence_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Edge(src="a", dst="b", relation="calls", confidence="MAYBE", provenance=_prov())


def test_provenance_requires_a_date():
    with pytest.raises(ValidationError):
        Provenance(source_file="x", verified_at="not-a-date")


def test_repo_minimal():
    r = Repo(id="team/api", path="/home/u/work/team/api")
    assert r.host is None and r.head_commit is None
    assert Repo.model_validate(r.model_dump()) == r
