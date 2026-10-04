"""The output-side anonymize rewrite (kb/anonymize.py), unit by unit.

The route-complete test drives the server; these pin the rewrite's own rules, including the
cases a canary of the `host:kind:id` form never exercises.
"""

from __future__ import annotations

from contextlake.kb.anonymize import Anonymizer, active, using

ISSUE = {"id": "atlassian:issue:ACME-123", "repo": "(external)", "kind": "issue",
         "name": "ACME-123", "title": "Outage at a customer", "url": "https://t.example/x",
         "summary": "prose", "status": "open"}


def _a():
    return Anonymizer(b"k" * 16)


def test_an_external_item_keeps_its_kind_and_status_and_loses_its_text():
    out = _a().rewrite({"nodes": [ISSUE]})["nodes"][0]
    assert out["kind"] == "issue" and out["status"] == "open" and out["repo"] == "(external)"
    assert {"title", "url", "summary"}.isdisjoint(out)
    assert "ACME" not in out["name"] and "ACME" not in out["id"]


def test_ids_stay_consistent_across_edges_and_routes_and_prose():
    out = _a().rewrite({
        "nodes": [ISSUE],
        "edges": [{"src": "fn", "dst": "atlassian:issue:ACME-123"}],
        "href": "#/symbol/atlassian:issue:ACME-123",
        "note": "Tracked in ACME-123.",
    })
    new_id = out["nodes"][0]["id"]
    assert out["edges"][0]["dst"] == new_id
    assert out["href"] == f"#/symbol/{new_id}"
    assert "ACME-123" not in out["note"] and out["note"].endswith(".")


def test_a_second_pass_changes_nothing():
    a = _a()
    once = a.rewrite({"nodes": [ISSUE], "note": "ACME-123"})
    assert a.rewrite(once) == once


def test_a_document_keeps_its_name_and_loses_its_body_and_source_url():
    adr = {"id": "adr1", "repo": "team/app", "kind": "adr", "name": "Use the ledger",
           "doc": "Deciders: someone", "file": "docs/adr/0001.md"}
    ingested = {"id": "@ingest:h:1", "repo": "@ingest:h", "kind": "document", "name": "Handbook",
                "file": "https://wiki.example/handbook", "snippet": "body"}
    out = _a().rewrite({"nodes": [adr, ingested]})["nodes"]
    assert out[0] == {"id": "adr1", "repo": "team/app", "kind": "adr", "name": "Use the ledger",
                      "file": "docs/adr/0001.md"}
    assert out[1] == {"id": "@ingest:h:1", "repo": "@ingest:h", "kind": "document",
                      "name": "Handbook"}


def test_code_and_plain_words_are_untouched():
    frame = {"id": "figma:design:7", "repo": "(external)", "kind": "design", "name": "Login"}
    fn = {"id": "fn", "repo": "team/app", "kind": "function", "name": "LoginForm",
          "doc": "Shows the Login screen."}
    out = _a().rewrite({"nodes": [frame, fn]})["nodes"]
    assert out[0]["name"] != "Login"
    assert out[1] == fn         # a plain word is replaced only where it stands alone


def test_two_runs_give_different_labels():
    assert (Anonymizer().rewrite({"nodes": [ISSUE]})["nodes"][0]["name"]
            != Anonymizer().rewrite({"nodes": [ISSUE]})["nodes"][0]["name"])


def test_the_active_anonymizer_is_scoped_to_its_block():
    a = _a()
    assert active() is None
    with using(a):
        assert active() is a
    assert active() is None


def test_build_site_anonymizes_without_an_active_anonymizer(tmp_path):
    """A caller that asks build_site to anonymize with no anonymizer active used to get
    pages that render connector names in plain text, with nothing failing."""
    from datetime import date

    from contextlake.kb import visualize as viz
    from contextlake.kb.ids import make_id
    from contextlake.kb.model import EXTERNAL_REPO, Confidence, Edge, Node, Provenance, Repo
    from contextlake.kb.state import check_schema
    from contextlake.kb.store.sqlite_store import SqliteStore

    s = SqliteStore(tmp_path / "index.sqlite")
    check_schema(s)
    prov = Provenance(source_file="x", verified_at=date(2026, 10, 5))
    s.upsert_repo(Repo(id="team/app", path=str(tmp_path)))
    s.upsert_nodes("team/app", [
        Node(id=make_id("repo", "team/app"), repo="team/app", kind="repo", name="team/app"),
        Node(id="svc", repo="team/app", kind="class", name="ForecastService")])
    s.upsert_nodes(EXTERNAL_REPO, [Node(id="zd:issue:77", repo=EXTERNAL_REPO, kind="issue",
                                        name="zq7linkhost:ticket:77")])
    s.upsert_edges("team/app", [Edge(src="svc", dst="zd:issue:77", relation="tracked_by",
                                     confidence=Confidence.EXTRACTED, provenance=prov)])
    try:
        assert active() is None
        out = viz.build_site(s, tmp_path / "site", anonymize=True)
        texts = [p.read_text(encoding="utf-8", errors="replace")
                 for p in out.rglob("*.html")]
        plain = viz.build_site(s, tmp_path / "plain")
        plain_texts = [p.read_text(encoding="utf-8", errors="replace")
                       for p in plain.rglob("*.html")]
    finally:
        s.close()
    assert any("zq7linkhost" in t for t in plain_texts), "control: the plain site shows it"
    assert not any("zq7linkhost" in t for t in texts)
