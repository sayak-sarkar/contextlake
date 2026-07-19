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


def test_unsupported_lang_noop():
    assert extract_web_flow("r", "app/x/page.py", b"", "python") == ([], [])
