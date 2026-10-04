"""The dashboard reads its language labels and diagram-tab gates from the registries.

`dashboard.js` used to carry two hand-copied tables:

* `LANG_LABELS`, 14 entries, against 28 in `visualize/styling._LANG_LABELS`. A repo in
  one of the other 14 languages got the two-letter fallback instead of its label.
  12 of those 14 rendered a wrong label (`bash` showed `BA`, not `SH`). `scala` and
  `swift` matched by chance.
* `DIAGRAM_FORMATS`, whose `avail` functions named the kinds that enable each tab. A
  kind added to the registry as a classifier would draw in the class diagram while the
  Classes tab stayed disabled.

Both now come from `data.dashboard_vocab()`, built from the registries on every call.
That function reaches the browser two ways, and these tests hold each one:

* live (`kb dashboard --serve`): `window.__CL_VOCAB__` is prepended to `/dashboard.js`.
* static (`--site`, including the `--sample` demo build): the snapshot carries a `vocab`
  key, so it is in `data.js` and `data.json`.

There is no third place. `dashboard.js` is loaded by `server.py` and by `site.py` and by
nothing else, and `--serve --sample` goes through `build_dashboard_server`.

The last block drives a real Chrome against a live server. It is the only check that the
browser draws what the registry says, so it carries the "registry gains a kind, the tab
follows" claim end to end.
"""

from __future__ import annotations

import dataclasses
import json
import re
import socket
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest
from test_graph_command import _chrome_binary

from contextlake.kb import visualize as viz
from contextlake.kb.dashboard import data as kbdata
from contextlake.kb.dashboard.server import build_dashboard_server
from contextlake.kb.dashboard.site import build_dashboard_site
from contextlake.kb.kinds import KIND_REGISTRY
from contextlake.kb.model import Node, Repo
from contextlake.kb.parse import ALL_LANGS
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize import styling

_STATIC = (Path(__file__).resolve().parents[2]
           / "src" / "contextlake" / "kb" / "dashboard" / "static")
REPO = "acme/app"


def _tabs_by_fmt() -> dict[str, dict]:
    return {t["fmt"]: t for t in kbdata.dashboard_vocab()["diagram_tabs"]}


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------
def test_language_labels_are_the_styling_table_not_a_copy(monkeypatch):
    """Add a language to the styling table and the dashboard vocabulary has it.

    This is the claim the old JS map broke: the map was a second table, so a new
    language reached the graph page and not the dashboard.
    """
    assert "fakelang" not in kbdata.dashboard_vocab()["lang_labels"]
    monkeypatch.setitem(styling._LANG_LABELS, "fakelang", "FK")
    assert kbdata.dashboard_vocab()["lang_labels"]["fakelang"] == "FK"


def test_language_labels_cover_every_parsed_language():
    """The dashboard-facing data names every language the parser can emit.

    `test_kind_registry_parity` pins the styling table against `ALL_LANGS`. This pins the
    same fact on the data the browser receives, so a future filter in `dashboard_vocab`
    cannot drop a language without a failure here.
    """
    missing = sorted(ALL_LANGS - set(kbdata.dashboard_vocab()["lang_labels"]))
    assert not missing, f"languages the parser emits with no dashboard label: {missing}"


def test_the_relation_graph_tab_has_no_kind_gate():
    """`kinds: null` means always enabled. Every other tab names the kinds that enable it."""
    tabs = _tabs_by_fmt()
    assert tabs["mermaid"]["kinds"] is None
    assert all(t["kinds"] for fmt, t in tabs.items() if fmt != "mermaid")


def test_tabs_cover_exactly_the_formats_the_server_will_render():
    """`diagram()` refuses a format outside `DIAGRAM_FORMATS`, so the two must agree."""
    assert tuple(t["fmt"] for t in kbdata.dashboard_vocab()["diagram_tabs"]) \
        == kbdata.DIAGRAM_FORMATS


def test_classes_and_data_model_gates_are_the_registry_flags():
    tabs = _tabs_by_fmt()
    assert set(tabs["classdiagram"]["kinds"]) == {k for k, s in KIND_REGISTRY.items()
                                                  if s.classifier}
    assert set(tabs["erdiagram"]["kinds"]) == {k for k, s in KIND_REGISTRY.items()
                                               if s.er_entity}


def test_a_kind_the_registry_gains_enables_its_tab(monkeypatch):
    """A new classifier kind and a new ER entity kind each reach their own gate only."""
    spec = KIND_REGISTRY["class"]
    monkeypatch.setitem(KIND_REGISTRY, "fake_classifier",
                        dataclasses.replace(spec, classifier=True))
    monkeypatch.setitem(KIND_REGISTRY, "fake_entity",
                        dataclasses.replace(spec, classifier=False, er_entity=True))
    tabs = _tabs_by_fmt()
    assert "fake_classifier" in tabs["classdiagram"]["kinds"]
    assert "fake_classifier" not in tabs["erdiagram"]["kinds"]
    assert "fake_entity" in tabs["erdiagram"]["kinds"]
    assert "fake_entity" not in tabs["classdiagram"]["kinds"]


# ---------------------------------------------------------------------------
# The gate must match what the renderer draws
# ---------------------------------------------------------------------------
_RENDERERS = {
    "classdiagram": viz.to_class_diagram,
    "statediagram": viz.to_state_diagram,
    "erdiagram": viz.to_er_diagram,
    "deploymentdiagram": viz.to_deployment_diagram,
}

# `to_deployment_diagram` also draws a `module` node when its lang is hcl. The gate leaves
# `module` out on purpose: a repo's kind counts carry no language, and every code language
# emits `module` nodes, so listing it would enable Deployment on every repo.
_DRAWN_BUT_NOT_GATED = {("deploymentdiagram", "module")}


def _one_node_payload(kind: str) -> dict:
    # lang is "hcl" so the deployment renderer's own language check cannot hide a kind
    # the gate lists.
    node = {"id": f"n_{kind}", "kind": kind, "name": f"{kind}_x",
            "qualified_name": f"Entity.{kind}_x", "lang": "hcl"}
    return {"nodes": [node], "edges": [], "meta": {}}


def test_each_tab_gate_matches_what_its_renderer_draws():
    """Both directions, over every registered kind.

    A kind in the gate that the renderer ignores enables an empty tab. A kind the
    renderer draws that the gate omits leaves a working diagram behind a disabled tab,
    which is the defect this module fixes for classifiers.

    The state and deployment gates have no registry flag, so `dashboard_vocab` holds them
    as literals. This test is what keeps those literals honest against the renderers.
    """
    tabs = _tabs_by_fmt()
    wrong = []
    for fmt, render in _RENDERERS.items():
        gate = set(tabs[fmt]["kinds"])
        for kind in KIND_REGISTRY:
            stats: dict = {}
            render(_one_node_payload(kind), stats=stats)
            assert "nodes" in stats, f"{fmt} did not report what it drew"
            drawn = stats["nodes"] > 0
            if (fmt, kind) in _DRAWN_BUT_NOT_GATED:
                assert drawn, f"stale exemption: {fmt} no longer draws {kind}"
                continue
            if drawn != (kind in gate):
                wrong.append((fmt, kind, "drawn" if drawn else "ignored",
                              "gated" if kind in gate else "not gated"))
    assert not wrong, f"gate and renderer disagree: {wrong}"


# ---------------------------------------------------------------------------
# The two delivery paths
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _empty_store(tmp_path):
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    return SqliteStore(store_dir / "index.sqlite"), store_dir


def _served_js(tmp_path) -> str:
    store, store_dir = _empty_store(tmp_path)
    port = _free_port()
    srv = build_dashboard_server(store, store_dir, host="127.0.0.1", port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/dashboard.js", timeout=10) as r:
            return r.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


def _live_vocab(js: str) -> dict:
    m = re.match(r"(?:window\.__CL_\w+__=.*;\n)*?window\.__CL_VOCAB__=(\{.*\});\n", js)
    assert m, "the served /dashboard.js carries no window.__CL_VOCAB__ assignment"
    return json.loads(m.group(1))


def test_live_server_prepends_the_vocabulary_to_dashboard_js(tmp_path, monkeypatch):
    monkeypatch.setitem(styling._LANG_LABELS, "fakelang", "FK")
    monkeypatch.setitem(KIND_REGISTRY, "fake_classifier",
                        dataclasses.replace(KIND_REGISTRY["class"], classifier=True))
    got = _live_vocab(_served_js(tmp_path))
    assert got == kbdata.dashboard_vocab()
    assert got["lang_labels"]["fakelang"] == "FK"
    assert "fake_classifier" in {t["fmt"]: t for t in got["diagram_tabs"]}["classdiagram"]["kinds"]


def test_static_export_carries_the_vocabulary_in_data_js_and_data_json(tmp_path, monkeypatch):
    monkeypatch.setitem(styling._LANG_LABELS, "fakelang", "FK")
    out = tmp_path / "out"
    build_dashboard_site(tmp_path / "store", out, sample=True)

    data_js = (out / "data.js").read_text(encoding="utf-8")
    prefix = "window.__CONTEXTLAKE__ = "
    assert data_js.startswith(prefix)
    in_js = json.loads(data_js[len(prefix):].rstrip().rstrip(";"))["vocab"]
    in_json = json.loads((out / "data.json").read_text(encoding="utf-8"))["vocab"]

    assert in_js == in_json == kbdata.dashboard_vocab()
    assert in_js["lang_labels"]["fakelang"] == "FK"


# ---------------------------------------------------------------------------
# No hand-copied list may come back into the script
# ---------------------------------------------------------------------------
def _js_code() -> str:
    src = (_STATIC / "dashboard.js").read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("//"))


def test_dashboard_js_holds_no_language_table_or_kind_gate():
    code = _js_code()
    assert not re.search(r"LANG_LABELS\s*=\s*\{", code), (
        "dashboard.js defines a language label table again; read VOCAB.lang_labels")
    assert not re.search(r"\bavail\s*:", code), (
        "dashboard.js gates a diagram tab with its own function again; "
        "read the kinds from VOCAB.diagram_tabs")
    # And it does read the vocabulary, from both delivery paths.
    assert "VOCAB.lang_labels" in code
    assert "VOCAB.diagram_tabs" in code
    assert "SNAP.vocab" in code
    assert "__CL_VOCAB__" in code


# ---------------------------------------------------------------------------
# The browser draws what the registry says
# ---------------------------------------------------------------------------
def _browser_store(tmp_path, *, kind: str, lang: str):
    """One repo whose graph shard holds three nodes of one kind and one language.

    `repo_brief` reads the graph shard, not sqlite, so the shard is written and then
    reindexed. A fixture that only called `upsert_nodes` would give an empty brief, and
    every gated tab would stay disabled whether or not the gate worked.
    """
    store, store_dir = _empty_store(tmp_path)
    nodes = [Node(id=f"{REPO}::n{i}", repo=REPO, kind=kind, name=f"n{i}",
                  file=f"src/f{i}.sh", line_start=i + 1, lang=lang) for i in range(3)]
    store.upsert_repo(Repo(id=REPO, path=str(tmp_path), head_commit="h1"))
    write_shard(store_dir, GraphShard(repo=REPO, head_commit="h1", nodes=nodes))
    reindex_shard(store, store_dir, REPO)
    store.mark_indexed(REPO, "h1", "2026-01-01T00:00:00Z")
    return store, store_dir


def _chrome_dom(tmp_path, url: str) -> str:
    profile = tempfile.mkdtemp(prefix="profile-", dir=tmp_path)
    proc = subprocess.run(
        [_chrome_binary(), "--headless", "--disable-gpu", "--no-sandbox",
         "--disable-dev-shm-usage", f"--user-data-dir={profile}",
         "--virtual-time-budget=30000", "--dump-dom", url],
        capture_output=True, text=True, timeout=300)
    return proc.stdout


def _dump_dom(tmp_path, store, store_dir, route: str) -> str:
    """Render one dashboard route in a real Chrome against a live server.

    The port is probed and then configured, never left at 0: the server pins the allowed
    Host header to `host:port`, so a server built with port 0 answers 403 to everything.
    """
    port = _free_port()
    srv = build_dashboard_server(store, store_dir, host="127.0.0.1", port=port,
                                 allow_mutations=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return _chrome_dom(tmp_path, f"http://127.0.0.1:{port}/#{route}")
    finally:
        srv.shutdown()
        srv.server_close()


def _tab_button(dom: str, fmt: str) -> str:
    m = re.search(rf'<button[^>]*data-fmt="{fmt}"[^>]*>', dom)
    assert m, f"no {fmt} tab in the dumped DOM"
    return m.group(0)


needs_chrome = pytest.mark.skipif(_chrome_binary() is None,
                                  reason="no Chrome/Chromium available to render the page")


@needs_chrome
def test_a_repo_in_a_language_the_old_map_lacked_shows_its_registry_label(tmp_path):
    """`bash` is one of the 14 languages the JS map never held: it showed `BA`, not `SH`."""
    store, store_dir = _browser_store(tmp_path, kind="function", lang="bash")
    dom = _dump_dom(tmp_path, store, store_dir, "/fleet")
    store.close()

    marks = re.findall(r'<span class="cl-lettermark" title="([^"]*)">([^<]*)</span>', dom)
    assert marks == [("bash", "SH")]


@needs_chrome
def test_a_static_export_opened_from_file_shows_the_registry_label_too(tmp_path):
    """The same label through the other delivery path: `data.js`, opened from `file://`.

    A static export cannot fetch, so this is the one place the snapshot's `vocab` key is
    read. The export is built from a real (temporary) store so the repo is in `bash`.
    """
    store, store_dir = _browser_store(tmp_path, kind="function", lang="bash")
    store.close()
    out = tmp_path / "site"
    build_dashboard_site(store_dir, out)

    dom = _chrome_dom(tmp_path, (out / "index.html").as_uri() + "#/fleet")
    marks = re.findall(r'<span class="cl-lettermark" title="([^"]*)">([^<]*)</span>', dom)
    assert marks == [("bash", "SH")]


@needs_chrome
def test_a_kind_the_registry_makes_a_classifier_enables_the_classes_tab(
        tmp_path, monkeypatch):
    """`field` is not a classifier today and was never in the JS list either way.

    Control first: with the real registry the Classes tab is disabled, so the later
    enabled state is caused by the registry change and not by the gate being absent.
    """
    store, store_dir = _browser_store(tmp_path, kind="field", lang="bash")
    route = f"/repo/{REPO.replace('/', '%2F')}?tab=diagrams"

    # Precondition: the brief the tab gates on carries this kind and nothing else.
    brief = kbdata.repo_detail(store, store_dir, REPO)["brief"]
    assert brief["kinds"] == {"field": 3}

    before = _dump_dom(tmp_path, store, store_dir, route)
    assert "disabled" in _tab_button(before, "classdiagram"), "control: tab should start disabled"
    assert "disabled" in _tab_button(before, "statediagram")

    monkeypatch.setitem(KIND_REGISTRY, "field",
                        dataclasses.replace(KIND_REGISTRY["field"], classifier=True))
    after = _dump_dom(tmp_path, store, store_dir, route)
    store.close()

    assert "disabled" not in _tab_button(after, "classdiagram")
    # Only the tab the registry change reaches moved.
    assert "disabled" in _tab_button(after, "statediagram")
    assert "disabled" in _tab_button(after, "erdiagram")
