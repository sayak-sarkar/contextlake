"""Tests for the `contextlake graph` visualizer (bounded subgraph + renderers)."""

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from contextlake.cli import main
from contextlake.kb import visualize as viz
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize import _CDN_URL

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "fixtures" / "sample-graph.json"


def _node(nid, repo="r", kind="function", name=None):
    return Node(id=nid, repo=repo, kind=kind, name=name or nid)


def _edge(src, dst, relation="calls"):
    return Edge(src=src, dst=dst, relation=relation, confidence=Confidence.EXTRACTED,
                provenance=Provenance(source_file="a.py", source_line=1,
                                      verified_at=date(2026, 6, 21)))


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    yield s
    s.close()


def _hub(store, leaves=200):
    nodes = [_node("H", kind="class")] + [_node(f"L{i}") for i in range(leaves)]
    store.upsert_nodes("r", nodes)
    store.upsert_edges("r", [_edge("H", f"L{i}") for i in range(leaves)])


# --- bounded extraction --------------------------------------------------

def test_extract_respects_fanout_cap(store):
    _hub(store, leaves=200)
    nodes, _ = viz.extract_subgraph(store, ["H"], hops=1, max_nodes=500, max_fanout=30)
    assert len(nodes) == 31  # the hub + at most max_fanout neighbours


def test_extract_respects_max_nodes_and_induces_edges(store):
    _hub(store, leaves=200)
    nodes, edges = viz.extract_subgraph(store, ["H"], hops=1, max_nodes=20, max_fanout=1000)
    assert len(nodes) == 20  # global node cap stops expansion
    ids = {n.id for n in nodes}
    # induced subgraph: no dangling edges to capped-out neighbours
    assert all(e.src in ids and e.dst in ids for e in edges)
    assert len(edges) == 19


def test_truncation_meta_is_honest(store):
    _hub(store, leaves=30)
    # overview/repo report a true total; extract reports truncation WITHOUT a total
    # (BFS early-stops, so a number would be a fabrication).
    rm = {}
    viz.repo_subgraph(store, "r", max_nodes=5, meta=rm)
    assert rm["truncated"] is True and rm["total"] == 31
    em = {}
    viz.extract_subgraph(store, ["H"], hops=1, max_nodes=5, max_fanout=100, meta=em)
    assert em["truncated"] is True and "total" not in em
    # an untruncated view says so, with no scary banner
    full = {}
    viz.repo_subgraph(store, "r", max_nodes=500, meta=full)
    assert full["truncated"] is False


def test_truncation_banner_reaches_html(store):
    _hub(store, leaves=2)
    pay = viz.to_payload(_payload(store)["nodes"], [],
                         {"mode": "repo", "truncated": True, "total": 99})
    html = viz.to_html(pay, cdn=True)
    assert 'id="trunc"' in html and '"truncated": true' in html and '"total": 99' in html


def test_extract_skips_unknown_seed(store):
    store.upsert_nodes("r", [_node("a")])
    nodes, edges = viz.extract_subgraph(store, ["does-not-exist", "a"], hops=1)
    assert {n.id for n in nodes} == {"a"}


def test_repo_subgraph_is_internal_only(store):
    store.upsert_repo(Repo(id="r", path="/r"))
    store.upsert_nodes("r", [_node("a", repo="r"), _node("b", repo="r")])
    store.upsert_nodes("other", [_node("x", repo="other")])
    store.upsert_edges("r", [_edge("a", "b"), _edge("a", "x")])  # a->x leaves the repo
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100)
    assert {n.id for n in nodes} == {"a", "b"}
    assert [(e.src, e.dst) for e in edges] == [("a", "b")]  # external edge excluded


def test_overview_aggregates_cross_repo(store):
    # the overview's cross-repo edges come from the package two-hop (publishes ⨝
    # depends_on), NOT raw imports: repoA publishes pkg, repoB depends_on it ->
    # repoB depends_on repoA.
    store.upsert_repo(Repo(id="repoA", path="/a"))
    store.upsert_repo(Repo(id="repoB", path="/b"))
    store.upsert_nodes("repoA", [_node("a1", repo="repoA")])
    store.upsert_nodes("repoB", [_node("b1", repo="repoB")])
    store.upsert_edges("repoA", [_edge("a1", "pkg", "publishes")])
    store.upsert_edges("repoB", [_edge("b1", "pkg", "depends_on")])
    nodes, edges = viz.overview_subgraph(store, max_nodes=50)
    assert {n["id"] for n in nodes} == {"repoA", "repoB"}
    assert all(n["kind"] == "repo" for n in nodes)
    dep = [e for e in edges if e["src"] == "repoB" and e["dst"] == "repoA"]
    assert len(dep) == 1 and dep[0]["relation"] == "depends_on"
    assert dep[0]["confidence"] == "INFERRED"  # manifest-derived, not ground truth


def test_overview_keeps_most_connected_not_alphabetical(store):
    # 'zzz' is a hub (sorts last alphabetically) linked to aaa/bbb/ccc; the trivial
    # repos each have degree 1. With max_nodes=2 the hub must survive, not be dropped
    # for an alphabetically-earlier trivial repo.
    for r in ("aaa", "bbb", "ccc", "zzz"):
        store.upsert_repo(Repo(id=r, path="/" + r))
        store.upsert_nodes(r, [_node(r + "1", repo=r)])
    # zzz publishes a package the other three depend on -> it's the connectivity hub
    store.upsert_edges("zzz", [_edge("zzz1", "pkgz", "publishes")])
    for r in ("aaa", "bbb", "ccc"):
        store.upsert_edges(r, [_edge(r + "1", "pkgz", "depends_on")])
    nodes, _ = viz.overview_subgraph(store, max_nodes=2)
    ids = {n["id"] for n in nodes}
    assert "zzz" in ids and len(ids) == 2  # the hub is kept despite sorting last


def test_overview_includes_every_repo_even_without_code(store):
    store.upsert_repo(Repo(id="empty/repo", path="/e"))   # registered, no parsed nodes
    store.upsert_repo(Repo(id="has/code", path="/h"))
    store.upsert_nodes("has/code", [_node("c1", repo="has/code")])
    nodes, _ = viz.overview_subgraph(store, max_nodes=50)
    by_id = {n["id"]: n for n in nodes}
    assert "empty/repo" in by_id and "has/code" in by_id  # one node per repo
    assert by_id["empty/repo"]["attrs"]["node_count"] == 0


# --- exporters -----------------------------------------------------------

def _payload(store):
    n, e = viz.extract_subgraph(store, ["H"], hops=1, max_nodes=10, max_fanout=5)
    return viz.to_payload(n, e, {"mode": "neighborhood"})


def test_json_export_shape(store):
    _hub(store, leaves=20)
    d = json.loads(viz.to_json(_payload(store)))
    assert set(d) == {"nodes", "edges", "meta"}
    assert d["meta"]["mode"] == "neighborhood"
    assert d["nodes"] and "id" in d["nodes"][0]


def test_dot_and_mermaid_export(store):
    _hub(store, leaves=20)
    p = _payload(store)
    assert viz.to_dot(p).startswith("digraph contextlake {")
    assert viz.to_mermaid(p).startswith("graph LR")


def test_html_is_offline_by_default(store):
    _hub(store, leaves=5)
    html = viz.to_html(_payload(store))
    assert _CDN_URL not in html        # offline default: no CDN reference
    assert len(html) > 100_000         # the vendored lib is inlined
    html_cdn = viz.to_html(_payload(store), cdn=True)
    assert _CDN_URL in html_cdn        # --cdn references the CDN
    # ...and does not inline the ~1MB cytoscape lib. The page's own JS/CSS (app shell,
    # minimap, semantic zoom, LOD labels, legend glyphs) is always inlined and sits
    # ~73KB; the bound just has to stay well under the lib size (>1MB) to catch a regression.
    assert len(html_cdn) < 90_000


def test_kind_icons_are_offline_data_uris_with_contrast():
    icons = viz._kind_icons()
    # one glyph per palette kind, including the flow nodes (endpoint/topic)
    assert {"file", "class", "function", "package", "repo", "endpoint", "topic"} <= set(icons)
    for kind, uri in icons.items():
        assert uri.startswith("data:image/svg+xml;utf8,"), kind   # inlined, no CDN/sprite fetch
        assert "%3Csvg" in uri                                    # percent-encoded SVG
    # contrast is chosen per node fill: white glyph on the dark navy repo node,
    # dark glyph on the light yellow module node — a single colour would vanish on one.
    assert "%23ffffff" in icons["repo"]
    assert "%230E2A33" in icons["module"]


def test_html_inlines_icon_map_token():
    html = viz.to_html({"nodes": [{"id": "a", "kind": "class", "name": "A"}], "edges": []})
    assert "__ICONS__" not in html and "var ICONS =" in html
    assert "data:image/svg+xml" in html


def test_only_architectural_edges_are_labelled():
    # the labelled-flow wiring ships in the inlined app.js
    html = viz.to_html({"nodes": [{"id": "a", "kind": "repo", "name": "A"}], "edges": []})
    assert '"label": edgeLabel' in html and "ARCH_RELS" in html
    block = re.search(r"var ARCH_RELS = \{([^}]*)\}", html, re.S).group(1)
    assert "calls_http" in block and "depends_on" in block   # architectural -> labelled
    assert "contains" not in block                           # structural -> stays clean


def test_overview_repo_carries_dominant_language(store):
    store.upsert_nodes("py-svc", [
        Node(id="a", repo="py-svc", kind="function", name="a", lang="python"),
        Node(id="b", repo="py-svc", kind="function", name="b", lang="python"),
        Node(id="c", repo="py-svc", kind="function", name="c", lang="c")])
    store.upsert_nodes("js-svc", [
        Node(id="x", repo="js-svc", kind="function", name="x", lang="javascript")])
    nodes, _ = viz.overview_subgraph(store, max_nodes=50)
    by_id = {n["id"]: n for n in nodes}
    assert by_id["py-svc"]["lang"] == "python"        # dominant of {python:2, c:1}
    assert by_id["js-svc"]["lang"] == "javascript"


def test_lang_icons_are_offline_lettermarks():
    li = viz._lang_icons()
    assert {"python", "javascript", "typescript", "csharp"} <= set(li)
    for uri in li.values():
        assert uri.startswith("data:image/svg+xml;utf8,") and "%3Ctext" in uri


def test_html_carries_node_detail_and_ui_controls(store):
    store.upsert_nodes("team/x", [Node(id="a", repo="team/x", kind="class", name="A",
                                       qualified_name="x.A", file="a.py", line_start=3),
                                  _node("b", repo="team/x")])
    store.upsert_edges("team/x", [_edge("a", "b")])
    n, e = viz.extract_subgraph(store, ["a"], hops=1)
    html = viz.to_html(viz.to_payload(n, e, {"mode": "neighborhood"}), cdn=True)
    # data the detail panel / search read from
    assert '"qn": "x.A"' in html and '"file": "a.py"' in html
    # the new UI affordances are present
    for control in ('id="search"', 'id="legend"', 'id="png"', 'id="info"', 'data-kind='):
        assert control in html, control


def test_html_layout_initial(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store), cdn=True, layout="grid")
    assert 'var LAYOUT = "grid"' in html
    assert viz.to_html(_payload(store), cdn=True, layout="nonsense").count('var LAYOUT = "cose"')


def test_html_carries_contextlake_branding(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store), cdn=True)
    assert 'class="glyph"' in html                    # the brand glyph is inlined
    assert 'context<span class="l">lake</span>' in html  # two-tone wordmark
    # the brand palette drives the styling
    for hexcolor in ("#0E2A33", "#137A8B", "#2BB3A3", "#EAF4F4"):
        assert hexcolor in html, hexcolor


# --- packaging -----------------------------------------------------------

def test_edge_detail_is_surfaced(store):
    from datetime import date

    from contextlake.kb.model import Provenance
    store.upsert_nodes("r", [_node("a", kind="class"), _node("b")])
    store.upsert_edges("r", [Edge(
        src="a", dst="b", relation="calls", confidence=Confidence.INFERRED,
        provenance=Provenance(source_file="o.py", source_line=12, verified_at=date(2026, 6, 21)),
        context="call", weight=3.0)])
    n, e = viz.extract_subgraph(store, ["a"], hops=1)
    pay = viz.to_payload(n, e, {"mode": "neighborhood"})
    # to_json must NOT throw on the verified_at date, and must carry full provenance
    d = json.loads(viz.to_json(pay))
    ed = d["edges"][0]
    assert ed["prov_file"] == "o.py" and ed["prov_line"] == 12 and ed["verified_at"] == "2026-06-21"
    assert ed["context"] == "call" and ed["confidence"] == "INFERRED" and ed["weight"] == 3.0
    html = viz.to_html(pay, cdn=True)
    assert 'id="edgelegend"' in html and 'data-rel="calls"' in html  # relationship legend/filter
    assert "var REL_COLORS" in html and "showEdgeInfo" in html       # edge inspector wired
    assert '"prov_file": "o.py"' in html                              # provenance reaches the page


def test_cytoscape_asset_is_packaged():
    from importlib.resources import files
    asset = files("contextlake.kb") / "static" / "cytoscape.min.js"
    assert asset.is_file()
    assert "cytoscape" in asset.read_text(encoding="utf-8")[:4000].lower()


def test_app_assets_are_packaged():
    # the visualizer's CSS/JS were extracted to static/ files; they must resolve.
    from importlib.resources import files
    css = files("contextlake.kb") / "static" / "app.css"
    js = files("contextlake.kb") / "static" / "app.js"
    assert css.is_file() and js.is_file()
    assert "--deepwater" in css.read_text(encoding="utf-8")        # a known rule
    assert "function edgeColor" in js.read_text(encoding="utf-8")  # a known function


def test_html_inlines_extracted_assets(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store))
    # the extracted CSS + JS are inlined into the single offline file...
    assert "--deepwater" in html and "function edgeColor" in html
    # ...and no asset placeholder survives in the output.
    assert "__APP_CSS__" not in html and "__APP_JS__" not in html
    import re
    assert not re.findall(r"__[A-Z][A-Z]+__", html)  # no residual placeholder token


def test_html_sibling_assets_reference_not_inline(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store), assets="sibling")
    # sibling mode references the shared files instead of inlining them
    assert '<link rel="stylesheet" href="app.css">' in html
    assert '<script src="app.js"></script>' in html
    assert '<script src="cytoscape.min.js"></script>' in html
    assert "--deepwater" not in html and "function edgeColor" not in html  # not inlined


def test_build_site_emits_cross_linked_offline_pages(store, tmp_path):
    store.upsert_repo(Repo(id="repoA", path="/a"))
    store.upsert_repo(Repo(id="repoB", path="/b"))
    store.upsert_nodes("repoA", [_node("a1", repo="repoA")])
    store.upsert_nodes("repoB", [_node("b1", repo="repoB")])
    store.upsert_edges("repoA", [_edge("a1", "pkg", "publishes")])
    store.upsert_edges("repoB", [_edge("b1", "pkg", "depends_on")])
    out = tmp_path / "site"
    viz.build_site(store, out)

    # one shared copy of each asset, plus index + overview + a page per repo
    for asset in ("cytoscape.min.js", "app.css", "app.js"):
        assert (out / asset).is_file()
    assert (out / "index.html").is_file() and (out / "overview.html").is_file()
    assert (out / "repo-repoA.html").is_file() and (out / "repo-repoB.html").is_file()

    overview = (out / "overview.html").read_text(encoding="utf-8")
    index = (out / "index.html").read_text(encoding="utf-8")
    assert '"href": "repo-repoA.html"' in overview   # repo node -> its page
    assert 'href="repo-repoA.html"' in index         # index lists the page
    # per-repo pages reference the shared lib rather than inlining ~1 MB each
    repo_html = (out / "repo-repoA.html").read_text(encoding="utf-8")
    assert '<script src="cytoscape.min.js"></script>' in repo_html
    assert "--deepwater" not in repo_html            # css linked, not inlined


def test_build_site_repos_filter(store, tmp_path):
    store.upsert_repo(Repo(id="team/repoA", path="/a"))
    store.upsert_repo(Repo(id="other/repoB", path="/b"))
    store.upsert_nodes("team/repoA", [_node("a1", repo="team/repoA")])
    store.upsert_nodes("other/repoB", [_node("b1", repo="other/repoB")])
    out = tmp_path / "site"
    viz.build_site(store, out, repos=["team/*"])
    # only the matching repo gets a page; the other is filtered out
    assert (out / "repo-team__repoA.html").is_file()
    assert not (out / "repo-other__repoB.html").exists()
    # the overview still lists every repo (fleet map stays whole)
    overview = (out / "overview.html").read_text(encoding="utf-8")
    assert '"id": "team/repoA"' in overview and '"id": "other/repoB"' in overview


def test_md_to_html_renders_and_escapes():
    h = viz._md_to_html(
        "# Title\n\nA `code` and **bold**.\n\n- one\n- two\n\n```\nx=1\n```\n\n<script>x</script>")
    assert "<h1>Title</h1>" in h and "<code>code</code>" in h and "<strong>bold</strong>" in h
    assert "<ul>" in h and "<li>one</li>" in h and "<pre><code>x=1" in h
    assert "&lt;script&gt;" in h and "<script>" not in h   # injection escaped


def test_md_to_html_no_href_attribute_breakout():
    # a crafted link URL with a quote must not break out of href="..." into a handler
    h = viz._md_to_html('[click](https://evil.com" onmouseover="alert(1))')
    assert 'onmouseover="alert' not in h        # no attribute breakout
    assert "&quot;" in h                         # the quote was escaped
    # a normal http(s) link still renders correctly
    h2 = viz._md_to_html("see [docs](https://example.com/x) now")
    assert '<a href="https://example.com/x" rel="noopener noreferrer">docs</a>' in h2


def test_build_site_emits_wiki_page_with_staleness(store, tmp_path):
    # store.path.parent is the kb dir; build_site reads <kb>/wiki/<slug>.md
    store.upsert_repo(Repo(id="team/api", path="/a", head_commit="abc123"))
    store.upsert_nodes("team/api", [_node("a1", repo="team/api")])
    wiki_dir = store.path.parent / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "team__api.md").write_text(
        "# team/api\n\nThe order service.\n\n"
        "*Generated from the knowledge graph of `team/api` at commit `abc123` on 2026-06-25.*\n")
    out = tmp_path / "site"
    viz.build_site(store, out)
    wiki_html = out / "wiki-team__api.html"
    assert wiki_html.is_file()
    body = wiki_html.read_text(encoding="utf-8")
    assert "The order service." in body and "fresh" in body   # commit matches -> fresh
    assert 'href="repo-team__api.html"' in body               # links back to the graph
    assert 'href="wiki-team__api.html"' in (out / "index.html").read_text(encoding="utf-8")


# --- live server ---------------------------------------------------------

def test_serve_endpoints(store):
    _hub(store, leaves=5)
    payload = _payload(store)
    port = _free_port()
    srv = viz.build_graph_server(store, payload, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        body = _get(base + "/")
        assert b"<html" in body.lower()
        nb = json.loads(_get(base + "/neighbors?id=H"))
        assert "nodes" in nb and "edges" in nb and nb["nodes"]
    finally:
        srv.shutdown()


def test_site_server_lazy_routes(store):
    store.upsert_repo(Repo(id="team/repoA", path="/a"))
    store.upsert_nodes("team/repoA", [_node("a1", repo="team/repoA")])
    port = _free_port()
    srv = viz.build_site_server(store, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # overview references the shared asset (not inlined); asset served separately
        overview = _get(base + "/")
        assert b'src="cytoscape.min.js"' in overview and b"--deepwater" not in overview
        assert b"--deepwater" in _get(base + "/app.css")
        # repo page is rendered on demand from the store
        repo = _get(base + "/repo-team__repoA.html")
        assert b"<html" in repo.lower() and b'"repo": "team/repoA"' in repo
        # unknown repo slug -> 404 (direct request; _get retries would mask it)
        try:
            urllib.request.urlopen(base + "/repo-nope.html", timeout=1)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, tries=50):
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - server may still be starting
            last = e
            time.sleep(0.05)
    raise AssertionError(f"request failed: {last}")


# --- CLI integration -----------------------------------------------------

def _kb_config(tmp_path) -> Path:
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    return cfg


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def test_cli_graph_formats_and_seeds(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    # --name seed -> mermaid to stdout (charge is reachable from OrderService)
    assert _run(["graph", "--config", str(cfg), "--name", "OrderService", "--kind", "class",
                 "--hops", "1", "--format", "mermaid"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("graph LR") and "OrderService" in out

    # --node seed -> json to a file
    target = tmp_path / "g.json"
    assert _run(["graph", "--config", str(cfg), "--node", "demo_app_orderservice",
                 "--format", "json", "--output", str(target)]) == 0
    d = json.loads(target.read_text())
    assert any(n["id"] == "demo_app_orderservice" for n in d["nodes"])

    # default html lands in --output
    html = tmp_path / "g.html"
    assert _run(["graph", "--config", str(cfg), "--search", "Order",
                 "--output", str(html)]) == 0
    assert html.exists() and _CDN_URL not in html.read_text()


def test_cli_graph_requires_a_seed(tmp_path):
    cfg = _kb_config(tmp_path)
    assert _run(["index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["graph", "--config", str(cfg)]) == 2  # no seed -> usage error


# --- class diagram --------------------------------------------------------

def _class_payload():
    prov = Provenance(source_file="m.py", source_line=1, verified_at=date(2026, 6, 21))
    nodes = [
        Node(id="base", repo="r", kind="class", name="BaseController"),
        Node(id="sub", repo="r", kind="class", name="OrdersController"),
        Node(id="iface", repo="r", kind="interface", name="Named"),
        Node(id="m1", repo="r", kind="method", name="handle",
             attrs={"signature": "(self, req)"}),
        Node(id="f", repo="r", kind="file", name="m.py"),
    ]
    edges = [
        Edge(src="base", dst="m1", relation="contains", confidence=Confidence.EXTRACTED,
             provenance=prov),
        Edge(src="sub", dst="base", relation="inherits", confidence=Confidence.INFERRED,
             provenance=prov),
        Edge(src="sub", dst="iface", relation="inherits", confidence=Confidence.INFERRED,
             provenance=prov),
        # a calls edge must NOT appear in a class diagram
        Edge(src="sub", dst="base", relation="calls", confidence=Confidence.INFERRED,
             provenance=prov),
    ]
    return viz.to_payload(nodes, edges)


def test_class_diagram_structure():
    out = viz.to_class_diagram(_class_payload())
    assert out.startswith("classDiagram")
    # each classifier is declared (files excluded)
    assert out.count("class c") == 3
    assert '["BaseController"]' in out and '["Named"]' in out
    assert "m.py" not in out                    # file node dropped
    # BaseController owns handle(self, req) as a member
    assert "+handle(self, req)" in out
    # extends -> solid, implements (interface) -> dotted
    assert "<|--" in out and "<|.." in out
    # a calls edge never becomes an association here
    assert out.count("<|") == 2 and "calls" not in out
    assert "-->" not in out


def test_class_diagram_interface_stereotype():
    out = viz.to_class_diagram(_class_payload())
    assert "<<interface>>" in out


def test_class_diagram_empty_when_no_classifiers():
    payload = viz.to_payload([Node(id="f", repo="r", kind="file", name="a.py")], [])
    out = viz.to_class_diagram(payload)
    assert out.startswith("classDiagram") and "no classes" in out
