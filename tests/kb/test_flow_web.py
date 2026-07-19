from contextlake.kb.flow.web import extract_web_flow, normalize_route
from contextlake.kb.model import Confidence


def _routes(nodes):
    return {n.name for n in nodes if n.kind == "route"}


# --- normalize_route -------------------------------------------------------

def test_normalize_route_collapses_dynamic():
    assert normalize_route("/flight-details/{}/{}") == "/flight-details/{}/{}"
    assert normalize_route("/orders/:id") == "/orders/{}"
    assert normalize_route("orders//view?x=1") == "/orders/view"


# --- Next.js App Router ----------------------------------------------------

def test_nextjs_page_route_basic():
    n, e = extract_web_flow(
        "repoA", "src/app/dashboard/page.js", b"export default function P(){}", "javascript")
    assert _routes(n) == {"/dashboard"}
    assert n[0].kind == "route" and n[0].repo == "repoA"
    assert n[0].file == "src/app/dashboard/page.js"
    assert e and e[0].relation == "defines_route" and e[0].confidence == Confidence.INFERRED


def test_nextjs_root_dynamic_group_catchall():
    # root page
    assert _routes(extract_web_flow("r", "app/page.tsx", b"", "tsx")[0]) == {"/"}
    # route group (agent) drops out; [flightId] collapses to {}
    grp = "src/app/(agent)/flight-details/[flightId]/page.js"
    assert _routes(extract_web_flow("r", grp, b"", "javascript")[0]) == {"/flight-details/{}"}
    # route.* API handler is DEFERRED this phase -> no route node
    api = "app/api/lpn/[...path]/route.js"
    assert _routes(extract_web_flow("r", api, b"", "javascript")[0]) == set()


def test_repo_scoped_ids_do_not_collide():
    a = extract_web_flow("repoA", "app/orders/page.js", b"", "javascript")[0][0]
    b = extract_web_flow("repoB", "app/orders/page.js", b"", "javascript")[0][0]
    assert a.id != b.id  # LOCKED: repo-scoped, else fleet-wide collision


def test_distinct_routes_get_distinct_ids():
    # /orders and /orders/{} are different routes; their ids must not collapse
    # (make_id would merge them if the whole path were one part).
    lst = extract_web_flow("r", "app/orders/page.js", b"", "javascript")[0]
    detail = extract_web_flow("r", "app/orders/[id]/page.js", b"", "javascript")[0]
    root = extract_web_flow("r", "app/page.js", b"", "javascript")[0]
    dyn_root = extract_web_flow("r", "app/[slug]/page.js", b"", "javascript")[0]
    ids = {lst[0].id, detail[0].id, root[0].id, dyn_root[0].id}
    assert len(ids) == 4
    assert {lst[0].name, detail[0].name} == {"/orders", "/orders/{}"}


def test_unsupported_lang_noop():
    assert extract_web_flow("r", "app/x/page.py", b"", "python") == ([], [])


# --- React Router v6 flat JSX ----------------------------------------------

def test_react_router_jsx_flat():
    src = b'''
      <Routes>
        <Route exact path="/" element={<Home/>} />
        <Route path="/flightpage" element={<FlightPage onX={h}/>} />
        <Route path="/:id" element={cond ? <A/> : <B/>} />
      </Routes>
    '''
    n, e = extract_web_flow("repoA", "src/App.js", src, "javascript")
    assert _routes(n) == {"/", "/flightpage", "/{}"}
    # component captured only when the element is a single simple <Name/>
    ctx = {ed.context for ed in e}
    assert "FlightPage" in ctx and "Home" in ctx
    assert None in ctx  # the ternary element has no clean component name


def test_react_router_ignores_object_form():
    # createBrowserRouter object form is DEFERRED (v2.40.0) -> no capture
    src = b'createBrowserRouter([{ path: "/x", Component: X, children: [] }])'
    assert extract_web_flow("r", "src/routes.ts", src, "typescript") == ([], [])


def test_web_skips_vendored_paths():
    src = b'<Route path="/demo" element={<Demo/>} />'
    p = "module-federation/apps/x/src/App.js"
    assert extract_web_flow("r", p, src, "javascript") == ([], [])
