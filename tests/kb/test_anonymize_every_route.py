"""`--anonymize` holds on every dashboard route and in every `--site` file.

Each surface that leaked had its own serializer that missed one field: the export's wiki
pages, the chat answers, and then connector link names and titles and ADR bodies on fourteen
routes (stability v2 tier D, D-1 and D-2). This drives the real server on every read route,
and reads every file a real `--site` export writes, with canaries planted where each of those
leaks lived. The same run without `--anonymize` must find every canary, or the fixture is not
reaching the field and the clean result proves nothing.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from contextlake.kb.dashboard.server import build_dashboard_server
from contextlake.kb.dashboard.site import build_dashboard_site
from contextlake.kb.ids import make_id
from contextlake.kb.model import EXTERNAL_REPO, Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

SERVER_PY = (Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb" / "dashboard"
             / "server.py")
REPO = "team/app"
REPO_NODE = make_id("repo", REPO)
ISSUE = "zendesk:issue:zq7linkhost:77"
ARTICLE = "zendesk:article:zq7linkhost:4242"
# Where each leak lived: a link name built from the external host, a title its users wrote,
# and an ADR body naming its decider with an email and a URL.
CANARIES = ("zq7linkhost", "zq7slug", "Dc5Qdecider", "dc5q@example", "ad63host",
            # A tracker key as a name (only replaced inside prose if keys count as
            # distinctive), its title, and the URLs an enriched and an ingested document came
            # from. An enrich document's body `snippet` is planted too, but no route serves
            # that field (the plain control never shows it), so it is not a canary here.
            "ACME-123", "Pw7Title", "zq9enrich", "zq8ingest",
            # A document named and identified by its URL (D-3), linked and unlinked.
            "zq5weburl", "zq4lonely")
KEY_ISSUE = "atlassian:issue:ACME-123"
_PROV = Provenance(source_file="x", verified_at=date(2026, 10, 5))


def _edge(src, dst, relation):
    return Edge(src=src, dst=dst, relation=relation, confidence=Confidence.EXTRACTED,
                provenance=_PROV)


def _store(tmp_path) -> Path:
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    adr_doc = ("Deciders: Dc5Qdecider <dc5q@example.test>. "
               "Background: https://ad63host.example.test/rfc/1")
    nodes = [
        Node(id=REPO_NODE, repo=REPO, kind="repo", name=REPO),
        Node(id="svc", repo=REPO, kind="class", name="ForecastService", lang="python",
             file="src/svc.py", line_start=1),
        Node(id="caller", repo=REPO, kind="function", name="run_cycle", lang="python",
             file="src/run.py", line_start=1),
        Node(id="adr_0001", repo=REPO, kind="adr", name="Use the ledger store",
             file="docs/adr/0001-ledger.md", attrs={"doc": adr_doc}),
    ]
    edges = [_edge("caller", "svc", "calls"), _edge("svc", "adr_0001", "references"),
             _edge("caller", "adr_0001", "references")]
    s.upsert_repo(Repo(id=REPO, path=str(tmp_path), head_commit="h1"))
    write_shard(store_dir, GraphShard(repo=REPO, head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, store_dir, REPO)
    s.mark_indexed(REPO, "h1", "2026-10-05T00:00:00Z")
    s.upsert_nodes(EXTERNAL_REPO, [
        Node(id=ISSUE, repo=EXTERNAL_REPO, kind="issue", name="zq7linkhost:ticket:77",
             attrs={"title": "zq7slug runbook", "status": "open",
                    "url": "https://zq7linkhost.zendesk.example.test/tickets/77"}),
        Node(id=ARTICLE, repo=EXTERNAL_REPO, kind="document", name="zq7linkhost:article:4242",
             attrs={"title": "zq7slug article",
                    "url": "https://zq7linkhost.zendesk.example.test/articles/4242"}),
        Node(id=KEY_ISSUE, repo=EXTERNAL_REPO, kind="issue", name="ACME-123",
             attrs={"title": "Pw7Title outage", "status": "open",
                    "url": "https://tracker.example.test/browse/ACME-123"}),
    ])
    s.upsert_edges(f"@connect:{REPO}", [
        _edge(REPO_NODE, ISSUE, "discussed_in"), _edge(REPO_NODE, ARTICLE, "documented_by"),
        _edge("svc", ISSUE, "tracked_by"), _edge(REPO_NODE, KEY_ISSUE, "tracked_by"),
        _edge("svc", KEY_ISSUE, "tracked_by"),
    ])
    enrich = f"@enrich:{REPO}"
    s.upsert_nodes(enrich, [Node(
        id=f"{enrich}:runbook", repo=enrich, kind="document", name="Runbook",
        file="https://zq9enrich.example.test/runbook",
        attrs={"source": "api", "snippet": "Pw9Snippet: page the on-call first"})])
    s.upsert_edges(enrich, [_edge(f"{enrich}:runbook", "svc", "mentions")])
    ingest = "@ingest:handbook"
    s.upsert_nodes(ingest, [Node(
        id=f"{ingest}:h1", repo=ingest, kind="document", name="Handbook",
        file="https://zq8ingest.example.test/handbook", attrs={"source": "api"})])
    # A page with no <title>: the web source and an MCP source name the document by its
    # URL, and every ingest and enrich id embeds `doc.id`, which is the URL (D-3). One is
    # linked to code, one is linked to nothing, so only a search reaches it.
    web = "@ingest:web"
    page = "https://zq5weburl.example.test/notes-page"
    s.upsert_nodes(web, [Node(id=f"{web}:{page}", repo=web, kind="document", name=page,
                              file=page, attrs={"source": "web"})])
    s.upsert_edges(web, [_edge("svc", f"{web}:{page}", "documented_by")])
    lone = "https://zq4lonely.example.test/notes-lone"
    s.upsert_nodes(enrich, [Node(id=f"{enrich}:{lone}", repo=enrich, kind="document",
                                 name=lone, file=lone, attrs={"source": "mcp"})])
    s.close()
    return store_dir


GETS = [
    "/api/overview", "/api/groups", "/api/health", "/api/relationships",
    "/api/impact?node=svc", "/api/impact/diagram?node=svc", "/api/path?from=caller&to=svc",
    "/api/search?q=article", "/api/search?q=ticket", "/api/search?q=ledger",
    "/api/search?q=runbook", "/api/search?q=handbook", "/api/search?q=outage",
    "/api/search?q=123",
    "/api/search?q=notes",
    f"/api/repo/{REPO}", f"/api/repo/{REPO}/rel", f"/api/repo/{REPO}/data-flow",
    f"/api/repo/{REPO}/diagram?format=mermaid", f"/api/repo/{REPO}/modules",
    f"/api/repo/{REPO}/wiki", f"/api/repo/{REPO}/docs",
    "/api/mcp", "/api/wiki/status", "/api/docs/status", "/api/wiki/estimate",
    "/api/settings", "/api/capabilities",
    "/neighbors?id=svc", f"/neighbors?id={REPO_NODE}", "/graph/neighbors?id=svc",
    "/graph/overview", "/graph/repo-team__app",
]
QUESTIONS = ["who owns team/app", "explain team/app", "what is ForecastService",
             "search ticket", "what tickets mention ForecastService",
             "123", "acme",     # the bare search route: reaches the tracker key
             "notes"]           # ...and the documents named by their address (D-3)
# No request carries a canary: a response that echoes the query would read as a leak.
# Mutation routes are off without --allow-mutations, and the CLI refuses that flag with
# --anonymize (test below), so an anonymized server never serves them.
MUTATION_ROUTES = {"/api/docs/generate", "/api/wiki/generate", "/api/repo/add",
                   "/api/mcp/serve", "/sync"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(store_dir, anonymize: bool, requests) -> dict[str, str]:
    s = SqliteStore(store_dir / "index.sqlite")
    port = _free_port()
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port, anonymize=anonymize)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    out = {}

    def fetch(req) -> str:
        # An error body is a response too, and goes through the same writer.
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - loopback
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.read().decode("utf-8", "replace")

    try:
        for key, make in requests:
            out[key] = fetch(make(base))
    finally:
        srv.shutdown()
        s.close()
    return out


def _bodies(store_dir, anonymize: bool, *, one_server_per_request: bool = False
            ) -> dict[str, str]:
    requests = [(path, lambda base, p=path: base + p) for path in GETS]
    requests += [(f"POST /api/chat {q}", lambda base, q=q: urllib.request.Request(
        base + "/api/chat", method="POST", data=json.dumps({"question": q}).encode(),
        headers={"Content-Type": "application/json"})) for q in QUESTIONS]
    if not one_server_per_request:
        return _serve(store_dir, anonymize, requests)
    out = {}
    for request in requests:
        out.update(_serve(store_dir, anonymize, [request]))
    return out


def _site(store_dir, out, anonymize: bool) -> dict[str, str]:
    build_dashboard_site(store_dir, out, anonymize=anonymize)
    return {str(p.relative_to(out)): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(out.rglob("*")) if p.is_file()}


def _hits(bodies: dict[str, str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for where, text in bodies.items():
        low = text.lower()
        for c in CANARIES:
            if c.lower() in low:
                found.setdefault(c, []).append(where)
    return found


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return _store(tmp_path)


def test_every_route_keeps_the_promise(store_dir):
    plain_bodies = _bodies(store_dir, anonymize=False)
    # Chat must be exercised on its own: the union control cannot tell that it was.
    assert any("ACME-123" in t for k, t in plain_bodies.items() if k.startswith("POST")), (
        "no chat question reached the tickets: the chat half of this test is vacuous")
    plain = _hits(plain_bodies)
    assert set(plain) == set(CANARIES), (
        f"positive control: the plain server never showed {set(CANARIES) - set(plain)}, "
        "so the fixture does not reach that field")
    leaked = _hits(_bodies(store_dir, anonymize=True))
    assert leaked == {}, f"--anonymize leaked: {leaked}"


def test_no_route_depends_on_an_earlier_request(store_dir):
    """Each request on a server of its own. The rewrite maps an id once it has seen the node
    it belongs to, so a route asked first has seen nothing: tier D's order probe got a raw
    document address from a search sent before any repo page (D-3)."""
    plain = _hits(_bodies(store_dir, anonymize=False, one_server_per_request=True))
    assert set(plain) == set(CANARIES), f"control: one server per request showed {sorted(plain)}"
    leaked = _hits(_bodies(store_dir, anonymize=True, one_server_per_request=True))
    assert leaked == {}, f"--anonymize leaked on a fresh server: {leaked}"


def test_every_site_file_keeps_the_promise(store_dir, tmp_path):
    plain = _hits(_site(store_dir, tmp_path / "plain", anonymize=False))
    assert {"zq7linkhost", "zq7slug", "Dc5Qdecider"} <= set(plain), (
        f"positive control: the plain export showed only {sorted(plain)}")
    leaked = _hits(_site(store_dir, tmp_path / "anon", anonymize=True))
    assert leaked == {}, f"--site --anonymize leaked: {leaked}"


def test_every_read_route_in_the_server_is_requested_here():
    """A route added to server.py without a request here fails this, rather than shipping
    with nobody checking what it sends under --anonymize. Each literal is matched the way the
    server routes it: an exact path, a prefix, a suffix after /api/repo/<id>, a /graph/ leaf.
    """
    src = SERVER_PY.read_text(encoding="utf-8")
    paths = [g.split("?")[0] for g in GETS] + ["/api/chat"]
    repo_base = f"/api/repo/{REPO}"
    missing = []
    for lit in re.findall(r'(?:path|parsed\.path)\s*==\s*"([^"]+)"', src):
        if lit not in MUTATION_ROUTES and lit not in paths:
            missing.append(lit)
    for lit in re.findall(r'(?<!\.)path\.startswith\(\s*"([^"]+)"', src):
        if not any(p.startswith(lit) and p != lit for p in paths):
            missing.append(lit + "*")
    for suffix in re.findall(r'rest\.endswith\(\s*"([^"]+)"', src):
        if repo_base + suffix not in paths:
            missing.append(repo_base + suffix)
    for leaf in re.findall(r'leaf\s*==\s*"([^"]+)"', src):
        if "/graph/" + leaf not in paths:
            missing.append("/graph/" + leaf)
    for leaf in re.findall(r'leaf\.startswith\(\s*"([^"]+)"', src):
        if not any(p.startswith("/graph/" + leaf) for p in paths):
            missing.append("/graph/" + leaf + "*")
    assert missing == [], f"routes with no request in this test: {missing}"


class _EchoLlm:
    """A provider that keeps every prompt it is sent and answers with it: whatever the
    prompt carried, the prose shows. `answer` is cut at 4,000 characters, so the test reads
    the kept prompts, which are what reaches a hosted provider."""

    name = "echo"

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt, *, system=None):
        self.prompts.append(prompt)
        return prompt


# What the plain run's prompts carry, measured: the ADR body, both tickets' names, and the
# two documents named by their address. No answer to these questions holds a connector
# item's title or the `file` URL of a titled document, so those canaries are covered by the
# read routes in the tests above, not here.
CHAT_QUESTIONS = [*QUESTIONS, "ticket"]
PROMPT_CANARIES = {"zq7linkhost", "Dc5Qdecider", "dc5q@example", "ad63host", "ACME-123",
                   "zq5weburl", "zq4lonely"}


@pytest.mark.parametrize("anonymize", [False, True])
def test_llm_chat_never_sends_hidden_text_to_the_provider(store_dir, monkeypatch, anonymize):
    """With --llm-chat the router's result goes into a prompt for the configured provider,
    and its prose comes back in `answer`. Under --anonymize only owners and the wiki were
    withheld, so ADR bodies and connector items reached the provider and its prose."""
    import contextlake.kb.embeddings as emb
    import contextlake.kb.llm.base as llm_base

    echo = _EchoLlm()
    monkeypatch.setattr(llm_base, "build_llm", lambda cfg: echo)
    monkeypatch.setattr(emb, "build_embedder", lambda cfg: None)
    s = SqliteStore(store_dir / "index.sqlite")
    port = _free_port()
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port,
                                 anonymize=anonymize, llm_chat=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        js = urllib.request.urlopen(base + "/dashboard.js", timeout=30).read().decode()  # noqa: S310
        token = json.loads(re.search(r"window\.__CL_TOKEN__=(\"[^\"]*\");", js).group(1))
        answers = []
        for q in CHAT_QUESTIONS:
            req = urllib.request.Request(
                base + "/api/chat", method="POST", data=json.dumps({"question": q}).encode(),
                headers={"Content-Type": "application/json", "X-Contextlake-Token": token})
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - loopback
                answers.append(json.loads(r.read())["answer"] or "")
    finally:
        srv.shutdown()
        s.close()
    assert len(echo.prompts) == len(answers), "a question never reached the provider"
    sent = _hits({f"prompt {i}": p for i, p in enumerate(echo.prompts)})
    shown = _hits({f"answer {i}": a for i, a in enumerate(answers)})
    if anonymize:
        assert sent == {} and shown == {}, f"the provider was sent {sent}; the prose showed {shown}"
    else:
        assert set(sent) == PROMPT_CANARIES, (
            f"control: the prompts carried {sorted(sent)}, not {sorted(PROMPT_CANARIES)}")
        assert {"Dc5Qdecider", "ACME-123"} <= set(shown), (
            f"control: the echo prose showed only {sorted(shown)}")


def test_mutations_are_refused_with_anonymize(tmp_path, monkeypatch, capsys):
    from contextlake.cli import main

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{(tmp_path / "kb").as_posix()}"\n')
    with pytest.raises(SystemExit) as e:
        main(["kb", "dashboard", "--serve", "--anonymize", "--allow-mutations",
              "--config", str(cfg)])
    assert e.value.code == 1
    cap = capsys.readouterr()
    assert "--allow-mutations refused with --anonymize" in cap.out + cap.err
