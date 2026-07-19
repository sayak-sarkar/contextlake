from datetime import date

import pytest

from contextlake.kb.arch.resolve import repo_http_flow_edges
from contextlake.kb.flow.http import _useful, extract_http_flow, normalize_path
from contextlake.kb.ids import make_id
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    yield s
    s.close()


def _rels(edges):
    return {(e.relation, e.context) for e in edges}


def test_normalize_path_strips_host_and_collapses_params():
    assert normalize_path("https://svc.internal/api/orders/123?q=1") == "/api/orders/{}"
    assert normalize_path("/orders/{id}") == "/orders/{}"
    assert normalize_path("orders/:orderId/items") == "/orders/{}/items"
    assert normalize_path("/orders/${id}") == "/orders/{}"


def test_useful_guard_rejects_generic_paths():
    assert _useful("/orders/{}")
    assert _useful("/api/orders")
    assert not _useful("/{}")        # would match almost anything
    assert not _useful("/")
    assert not _useful("/{}/{}")


def test_extract_python_fastapi_and_requests():
    src = b'''
@app.get("/orders/{id}")
def get_order(id): ...

def fetch():
    return requests.post("https://oms.internal/orders")
'''
    nodes, edges = extract_http_flow("r", "api.py", src, "python")
    assert "/orders/{}" in {n.name for n in nodes if n.kind == "endpoint"}
    assert ("exposes", "GET") in _rels(edges)
    assert ("calls_http", "POST") in _rels(edges)
    assert all(e.confidence == Confidence.INFERRED for e in edges)


def test_extract_csharp_aspnet_and_httpclient():
    src = b'''
[HttpGet("api/orders/{id}")]
public Order Get(int id) { }

var r = await _http.GetAsync("https://oms/api/orders/42");
app.MapPost("/api/checkin", Handler);
'''
    nodes, edges = extract_http_flow("r", "Ctrl.cs", src, "csharp")
    eps = {n.name for n in nodes if n.kind == "endpoint"}
    assert "/api/orders/{}" in eps and "/api/checkin" in eps
    assert ("exposes", "GET") in _rels(edges)
    assert ("calls_http", "GET") in _rels(edges)


def test_extract_express_routes_and_axios():
    src = b'''
router.get('/api/flights/:id', handler);
const r = await axios.post('https://svc/api/flights', body);
'''
    nodes, edges = extract_http_flow("r", "routes.ts", src, "typescript")
    assert "/api/flights/{}" in {n.name for n in nodes if n.kind == "endpoint"}
    assert ("exposes", "GET") in _rels(edges)
    assert ("calls_http", "POST") in _rels(edges)


def test_unsupported_language_is_noop():
    assert extract_http_flow("r", "a.go", b"http.Get(\"/x\")", "go") == ([], [])


def test_http_flow_two_hop_resolves_caller_to_exposer(store):
    ep = make_id("endpoint", "/orders/{}")
    file_a = make_id("repoA", "Ctrl.cs")   # exposer
    file_b = make_id("repoB", "Client.cs")  # caller
    prov = Provenance(source_file="x", source_line=1, verified_at=date.today())
    store.upsert_nodes("repoA", [
        Node(id=file_a, repo="repoA", kind="file", name="Ctrl.cs"),
        Node(id=ep, repo="repoA", kind="endpoint", name="/orders/{}")])
    store.upsert_nodes("repoB", [Node(id=file_b, repo="repoB", kind="file", name="Client.cs")])
    store.upsert_edges("repoA", [Edge(src=file_a, dst=ep, relation="exposes",
                                      confidence=Confidence.INFERRED, provenance=prov)])
    store.upsert_edges("repoB", [Edge(src=file_b, dst=ep, relation="calls_http",
                                      confidence=Confidence.INFERRED, provenance=prov)])
    flow = repo_http_flow_edges(store)
    assert len(flow) == 1
    e = flow[0]
    # request flows from the caller (repoB) to the exposer (repoA)
    assert e["src"] == "repoB" and e["dst"] == "repoA"
    assert e["relation"] == "flow" and e["context"] == "http"
    assert e["confidence"] == "INFERRED" and e["weight"] == 1


def test_http_flow_ignores_same_repo(store):
    ep = make_id("endpoint", "/internal/ping")
    f1 = make_id("repoA", "Ctrl.cs")
    f2 = make_id("repoA", "Client.cs")
    prov = Provenance(source_file="x", source_line=1, verified_at=date.today())
    store.upsert_nodes("repoA", [
        Node(id=f1, repo="repoA", kind="file", name="Ctrl.cs"),
        Node(id=f2, repo="repoA", kind="file", name="Client.cs"),
        Node(id=ep, repo="repoA", kind="endpoint", name="/internal/ping")])
    store.upsert_edges("repoA", [
        Edge(src=f1, dst=ep, relation="exposes",
             confidence=Confidence.INFERRED, provenance=prov),
        Edge(src=f2, dst=ep, relation="calls_http",
             confidence=Confidence.INFERRED, provenance=prov)])
    # a repo calling its own endpoint is not cross-repo flow
    assert repo_http_flow_edges(store) == []


# --- Next.js App Router API route handlers (route.ts) -----------------------

def test_nextjs_api_route_handler_exposes_endpoints():
    src = b"""
    export async function GET(req) { return Response.json({}) }
    export async function POST(req) { return Response.json({}) }
    """
    path = "src/app/api/orders/[orderId]/bags/route.ts"
    n, e = extract_http_flow("repoA", path, src, "typescript")
    assert "/api/orders/{}/bags" in {nn.name for nn in n if nn.kind == "endpoint"}
    rels = {(ed.relation, ed.context) for ed in e}
    assert ("exposes", "GET") in rels and ("exposes", "POST") in rels


def test_nextjs_page_file_is_not_an_api_route():
    # an exported GET in a page.tsx is not a Next.js route handler
    n, _ = extract_http_flow("r", "src/app/x/page.tsx", b"export function GET(){}", "typescript")
    assert not [nn for nn in n if nn.kind == "endpoint"]


def test_nextjs_api_route_app_root_anchoring():
    # a real /api/app path must not collapse to / via last-app anchoring
    n, _ = extract_http_flow("r", "app/api/app/route.ts", b"export function GET(){}", "typescript")
    assert "/api/app" in {nn.name for nn in n if nn.kind == "endpoint"}
