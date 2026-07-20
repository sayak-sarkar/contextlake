from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
from contextlake.kb.flow.web import extract_web_flow, normalize_route
from contextlake.kb.model import Confidence
from contextlake.kb.parse import index_repo_dir


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


# --- indexer integration ---------------------------------------------------

def test_index_repo_dir_emits_route_nodes(tmp_path):
    app = tmp_path / "src" / "app" / "dashboard"
    app.mkdir(parents=True)
    (app / "page.js").write_text("export default function P(){ return null }\n")
    (tmp_path / "src").joinpath("App.js").write_text(
        '<Routes><Route path="/orders" element={<Orders/>} /></Routes>\n')
    shard = index_repo_dir(str(tmp_path), "repoA")
    routes = {n.name for n in shard.nodes if n.kind == "route"}
    assert routes == {"/dashboard", "/orders"}
    assert "route" in EMBEDDABLE_KINDS


def test_index_skips_next_build_output(tmp_path):
    # real source route is kept; the .next/ build-output mirror is skipped
    src_app = tmp_path / "src" / "app" / "dashboard"
    src_app.mkdir(parents=True)
    (src_app / "page.js").write_text("export default function P(){}\n")
    built = tmp_path / ".next" / "server" / "app" / "phantom"
    built.mkdir(parents=True)
    (built / "page.js").write_text("export default function P(){}\n")
    shard = index_repo_dir(str(tmp_path), "repoA")
    routes = {n.name for n in shard.nodes if n.kind == "route"}
    assert routes == {"/dashboard"}


def test_nextjs_app_root_not_last_occurrence():
    # a real /app or /settings/app route must not collapse to / (Important #1)
    assert _routes(extract_web_flow("r", "app/app/page.tsx", b"", "tsx")[0]) == {"/app"}
    seg = "app/settings/app/page.js"
    assert _routes(extract_web_flow("r", seg, b"", "javascript")[0]) == {"/settings/app"}
    # a monorepo package literally named app is not the router root
    mono = "packages/app/src/app/orders/page.tsx"
    assert _routes(extract_web_flow("r", mono, b"", "tsx")[0]) == {"/orders"}
    # a page.js not under an app-router root is not a route
    assert _routes(extract_web_flow("r", "components/app/x/page.js", b"", "javascript")[0]) == set()


def test_root_and_catchall_get_distinct_ids():
    src = b'<Route path="/" element={<Home/>} /><Route path="*" element={<NF/>} />'
    n = extract_web_flow("r", "src/App.js", src, "javascript")[0]
    assert {nn.name for nn in n} == {"/", "/*"}
    assert len({nn.id for nn in n}) == 2  # not collapsed to one id


# --- Angular route tables (tree-sitter AST) --------------------------------

def test_angular_flat_routes():
    src = b'''
      const routes: Routes = [
        { path: "", component: WelcomeComponent },
        { path: "login", component: LoginComponent },
        { path: "personal", component: ProfileComponent, canActivate: [AuthGuard] },
      ];
    '''
    n, e = extract_web_flow("r", "src/app/app-routing.module.ts", src, "typescript")
    assert _routes(n) == {"/", "/login", "/personal"}
    assert all(nn.kind == "route" and nn.repo == "r" for nn in n)


def test_angular_nested_children_compose():
    src = b'''
      const routes: Routes = [
        { path: "hotels", children: [
          { path: "", component: HotelsComponent },
          { path: "hotel-details/:id", component: HotelDetailsComponent },
        ]},
      ];
    '''
    got = _routes(extract_web_flow("r", "app-routing.module.ts", src, "typescript")[0])
    assert got == {"/hotels", "/hotels/hotel-details/{}"}


def test_angular_redirect_wildcard_loadchildren():
    src = b'''
      const routes: Routes = [
        { path: "", redirectTo: "/dashboard", pathMatch: "full" },
        { path: "dashboard", component: Landing },
        { path: "tenant", loadChildren: () => import("./tenant.module").then(m => m.TenantModule) },
        { path: "legacy", loadChildren: "app/legacy/legacy.module" },
        { path: "**", component: NotFound },
      ];
    '''
    # redirectTo skipped; loadChildren emits mount path (no recursion); ** -> /*
    got = _routes(extract_web_flow("r", "app-routing.module.ts", src, "typescript")[0])
    assert got == {"/dashboard", "/tenant", "/legacy", "/*"}


def test_angular_forroot_inline_array():
    src = b'RouterModule.forRoot([{ path: "x", component: X }])'
    assert _routes(extract_web_flow("r", "app.module.ts", src, "typescript")[0]) == {"/x"}


def test_angular_no_phantom_from_non_route_path_objects():
    src = b'const config = { path: "/tmp/build", output: "dist" }; const opts = { path: "x" };'
    assert extract_web_flow("r", "webpack.config.ts", src, "typescript") == ([], [])


def test_angular_prefilter_skips_unrelated_ts():
    src = b'export const x = [{path:"a"}]'
    assert extract_web_flow("r", "util.ts", src, "typescript") == ([], [])
