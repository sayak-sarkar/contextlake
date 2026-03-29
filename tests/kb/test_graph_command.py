"""Tests for the `contextlake graph` visualizer (bounded subgraph + renderers)."""

import json
import socket
import threading
import time
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
    store.upsert_repo(Repo(id="repoA", path="/a"))
    store.upsert_repo(Repo(id="repoB", path="/b"))
    store.upsert_nodes("repoA", [_node("a1", repo="repoA")])
    store.upsert_nodes("repoB", [_node("b1", repo="repoB")])
    store.upsert_edges("repoA", [_edge("a1", "b1", "imports"), _edge("a1", "b1", "imports")])
    nodes, edges = viz.overview_subgraph(store, max_nodes=50)
    assert {n["id"] for n in nodes} == {"repoA", "repoB"}
    assert all(n["kind"] == "repo" for n in nodes)
    cross = [e for e in edges if e["src"] == "repoA" and e["dst"] == "repoB"]
    assert len(cross) == 1 and cross[0]["relation"] == "imports" and cross[0]["weight"] == 2


def test_overview_keeps_most_connected_not_alphabetical(store):
    # 'zzz' is a hub (sorts last alphabetically) linked to aaa/bbb/ccc; the trivial
    # repos each have degree 1. With max_nodes=2 the hub must survive, not be dropped
    # for an alphabetically-earlier trivial repo.
    for r in ("aaa", "bbb", "ccc", "zzz"):
        store.upsert_repo(Repo(id=r, path="/" + r))
        store.upsert_nodes(r, [_node(r + "1", repo=r)])
    store.upsert_edges("zzz", [_edge("zzz1", "aaa1", "imports"),
                               _edge("zzz1", "bbb1", "imports"),
                               _edge("zzz1", "ccc1", "imports")])
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
    assert len(html_cdn) < 50_000      # ...and does not inline the lib


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
