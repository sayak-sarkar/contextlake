"""Reverse blast-radius / change-impact (`impact` verb + shared blast_radius)."""

from datetime import date

import pytest

from contextlake.cli import main
from contextlake.kb.impact import blast_radius
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))


def _edge(src, dst):
    return Edge(src=src, dst=dst, relation="calls",
                confidence=Confidence.EXTRACTED, provenance=_PROV)


def _store(tmp_path):
    """A -calls-> S, B -calls-> A, C -calls-> B (a 3-deep reverse chain into S)."""
    s = SqliteStore(tmp_path / "index.sqlite")
    s.upsert_nodes("r", [Node(id=i, repo="r", kind="function", name=i)
                         for i in ("S", "A", "B", "C")])
    s.upsert_edges("r", [_edge("A", "S"), _edge("B", "A"), _edge("C", "B")])
    return s


def test_blast_radius_walks_incoming_to_depth(tmp_path):
    s = _store(tmp_path)
    try:
        hits, trunc = blast_radius(s, "S", hops=2)
        assert {h.name for h in hits} == {"A", "B"}     # C is hop 3, beyond hops=2
        assert not trunc
        assert next(h.hop for h in hits if h.name == "A") == 1
        assert next(h.hop for h in hits if h.name == "B") == 2
    finally:
        s.close()


def test_blast_radius_respects_limit_and_reports_truncation(tmp_path):
    s = _store(tmp_path)
    try:
        hits, trunc = blast_radius(s, "S", hops=5, limit=1)
        assert len(hits) == 1 and trunc
    finally:
        s.close()


def test_cmd_impact_cli_lists_dependents(tmp_path, capsys):
    _store(tmp_path).close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path}"\n')
    with pytest.raises(SystemExit) as e:
        main(["impact", "S", "--config", str(cfg), "--hops", "3"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Impact of changing" in out and "A" in out


def test_cmd_impact_usage_error_without_target():
    with pytest.raises(SystemExit) as e:
        main(["impact"])
    assert e.value.code == 2
