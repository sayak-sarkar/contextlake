"""Reachability probes: can a code symbol actually *reach* external knowledge?

Every other test in this area asserts on the edge list a pipeline returns. That
proves the edge was built; it does not prove the edge is traversable once
persisted, which is the entire claim of the knowledge-consolidation work (before
it, connector content hung off an orphan `repo_*` node and a design was
unreachable from a code symbol at 3 hops).

So each test here runs the real pipeline, persists its output exactly the way
`cmd_connect` does (into the isolated `@connect:<repo>` partition), and then
walks the graph with the same `_expand` BFS that hybrid retrieval uses. The
assertion is always "the external node is in the visited set", never "an edge
with the right shape exists".
"""

from __future__ import annotations

import contextlake.kb.connectors.orchestrate as orch
from contextlake.kb.connectors.text_match import link_documents_to_symbols
from contextlake.kb.embeddings.hybrid import _expand
from contextlake.kb.model import Node
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "team/api"
PART = orch.connect_partition(REPO)


def _store_with_symbols(*nodes: Node) -> SqliteStore:
    store = SqliteStore(":memory:")
    store.upsert_nodes(REPO, list(nodes))
    return store


def _persist(store, nodes, edges) -> None:
    """Exactly what `cmd_connect` does with an enricher's output."""
    store.upsert_nodes(PART, nodes)
    store.upsert_edges(PART, edges)


def _reaches(store, seed_id: str, target_id: str, hops: int = 1) -> bool:
    visited, _ = _expand(store, [seed_id], hops=hops)
    return target_id in visited


class _StubGitLab:
    name = "gl"

    def fetch(self, repo_id):
        return ([{"iid": 7, "title": "Fix charge rounding", "state": "opened",
                  "web_url": "https://git.example/mr/7"}], [])

    def fetch_changes(self, repo_id, mr_iid):
        return ["pay.py"]


class _StubFigma:
    hosts = ("figma.com",)

    def fetch_metadata(self, file_key, **kw):
        return {"name": "Payer"}


class _StubSlack:
    hosts = ("slack.com",)

    def verify(self, channel, **kw):
        return True

    def fetch_messages(self, channel, **kw):
        return ["charge() is throwing again on retries"]


def test_code_file_reaches_its_merge_request_in_one_hop():
    store = _store_with_symbols(
        Node(id="team_api_pay_py", repo=REPO, kind="file", name="pay.py", file="pay.py"),
    )
    try:
        nodes, edges = orch.enrich_repo_gitlab(_StubGitLab(), REPO, store)
        _persist(store, nodes, edges)
        mr = next(n for n in nodes if n.kind == "mr")
        assert _reaches(store, "team_api_pay_py", mr.id)
    finally:
        store.close()


def test_code_symbol_reaches_its_design_in_one_hop():
    store = _store_with_symbols(
        Node(id="team_api_payer", repo=REPO, kind="class", name="Payer", file="pay.py"),
    )
    try:
        nodes, edges = orch.enrich_repo_figma(
            _StubFigma(), REPO, store, links=["https://www.figma.com/design/Xy9/Flow"])
        _persist(store, nodes, edges)
        design = next(n for n in nodes if n.kind == "design")
        assert _reaches(store, "team_api_payer", design.id)
    finally:
        store.close()


def test_code_symbol_reaches_the_channel_discussing_it_in_one_hop():
    store = _store_with_symbols(
        Node(id="team_api_charge", repo=REPO, kind="function", name="charge", file="pay.py"),
    )
    try:
        nodes, edges = orch.enrich_repo_slack(
            _StubSlack(), REPO, store,
            links=["https://acme.slack.com/archives/C0123ABCD"])
        _persist(store, nodes, edges)
        channel = next(n for n in nodes if n.kind == "channel")
        assert _reaches(store, "team_api_charge", channel.id)
    finally:
        store.close()


def test_code_symbol_reaches_a_document_that_mentions_it_in_one_hop():
    """Covers the ingest/enrich/wiki leg -- all three share this exact body
    (`link_documents_to_symbols`), differing only in the relation name and
    whether the repo-level fallback edge is kept."""
    store = _store_with_symbols(
        Node(id="team_api_charge", repo=REPO, kind="function", name="charge", file="pay.py"),
    )
    try:
        doc = Node(id="doc_runbook", repo="@ingest:cli", kind="document", name="runbook.md")
        edges = link_documents_to_symbols(
            store, REPO, [doc], ["If charge() fails, check the gateway logs."],
            "documented_by", "ingest")
        store.upsert_nodes("@ingest:cli", [doc])
        store.upsert_edges("@ingest:cli", edges)
        assert _reaches(store, "team_api_charge", doc.id)
    finally:
        store.close()


def test_unrelated_symbol_does_not_reach_the_external_node():
    """The probe must be able to fail: a symbol the content never mentions
    stays unreachable, so a passing probe above means a real edge and not a
    catch-all that links everything to everything."""
    store = _store_with_symbols(
        Node(id="team_api_charge", repo=REPO, kind="function", name="charge", file="pay.py"),
        Node(id="team_api_unrelated", repo=REPO, kind="function", name="render_invoice",
             file="invoice.py"),
    )
    try:
        nodes, edges = orch.enrich_repo_slack(
            _StubSlack(), REPO, store,
            links=["https://acme.slack.com/archives/C0123ABCD"])
        _persist(store, nodes, edges)
        channel = next(n for n in nodes if n.kind == "channel")
        assert _reaches(store, "team_api_charge", channel.id)
        assert not _reaches(store, "team_api_unrelated", channel.id)
    finally:
        store.close()
