"""Tests for ADR/decision-record surfacing (kb/adr.py)."""

from contextlake.kb.adr import is_adr_path, parse_adr
from contextlake.kb.parse import index_repo_dir


def test_is_adr_path_matches_common_conventions():
    assert is_adr_path("docs/adr/0001-use-postgres.md")
    assert is_adr_path("docs/decisions/0002-use-kafka.md")
    assert is_adr_path("decisions/foo.md")
    assert is_adr_path("adr/bar.md")
    assert is_adr_path("ADR/CAPS-DIR.md")  # directory match is case-insensitive


def test_is_adr_path_rejects_unrelated_markdown():
    assert not is_adr_path("README.md")
    assert not is_adr_path("docs/guide.md")
    assert not is_adr_path("docs/adr/0001-use-postgres.py")  # right dir, wrong ext


def test_parse_adr_extracts_title_from_h1_heading():
    src = b"# Use PostgreSQL for the primary store\n\nBecause it's boring and reliable.\n"
    nodes = parse_adr("r", "docs/adr/0001-use-postgres.md", src)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.kind == "adr"
    assert n.name == "Use PostgreSQL for the primary store"
    assert n.repo == "r"
    assert n.file == "docs/adr/0001-use-postgres.md"
    assert n.qualified_name == "docs/adr/0001-use-postgres.md"
    assert "boring and reliable" in n.attrs["doc"]


def test_parse_adr_falls_back_to_filename_when_no_heading():
    src = b"No H1 in this one, just prose about the decision.\n"
    nodes = parse_adr("r", "docs/adr/0007-use-kafka-for-events.md", src)
    assert nodes[0].name == "Use kafka for events"  # number stripped, dashes -> spaces


def test_parse_adr_returns_nothing_for_an_empty_file():
    assert parse_adr("r", "docs/adr/0001-empty.md", b"   \n") == []


def test_parse_adr_truncates_a_very_long_body():
    src = b"# Title\n\n" + b"x" * 5000
    nodes = parse_adr("r", "docs/adr/0001-long.md", src)
    assert len(nodes[0].attrs["doc"]) <= 2000


def test_index_repo_dir_surfaces_an_adr_as_a_first_class_node(tmp_path):
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-use-postgres.md").write_text(
        "# Use PostgreSQL for the primary store\n\nBecause it's boring and reliable.\n")
    (tmp_path / "README.md").write_text("# My repo\n\nJust a normal readme.\n")

    shard = index_repo_dir(str(tmp_path), "acme/api")

    adrs = [n for n in shard.nodes if n.kind == "adr"]
    assert len(adrs) == 1
    assert adrs[0].name == "Use PostgreSQL for the primary store"
    assert adrs[0].repo == "acme/api"  # first-class node in the repo's own shard,
    # not a side-channel @enrich:/@ingest: pseudo-repo partition
    assert not any(n.kind == "adr" and n.file == "README.md" for n in shard.nodes)


def test_index_repo_dir_ignores_a_plain_docs_markdown_file(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# User guide\n\nHow to use this thing.\n")
    shard = index_repo_dir(str(tmp_path), "r")
    assert not any(n.kind == "adr" for n in shard.nodes)
