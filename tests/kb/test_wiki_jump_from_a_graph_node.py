"""V3: reaching a node's prose from the architecture graph.

The roadmap called this a "diagram-node to wiki-SECTION jump". The section half of
that has no target, and the real generated pages say so. Their headings are
page-level prose -- Overview, Setup & Run, Architecture, Dependencies, Gotchas --
and `wiki/generate.py`'s `_SECTIONS_INSTRUCTION` tells the model to omit any it has
nothing to say for. Nothing in a generated page is about one symbol, and no named
heading is guaranteed to exist. Computing an anchor would produce a link that lands
silently at the top of the page, which reads as working.

So the jump lands on a PAGE, and the finest page the system generates is the
per-subsystem one. A node carries its `file`, the dashboard knows which subsystems
have a page on disk, and the file resolves to the narrowest of those that contains
it. That is the whole feature.

What these tests run, rather than search for:

* `build_site` and the live `/graph/*` route, both driven, both asked what the
  rendered page actually carries.
* `wikiAction` in a real browser, through the shipped `showInfo`, by tapping a node.
* `moduleForFile` in a real browser, extracted from the shipped `dashboard.js` so a
  later edit to it is what runs here -- not a copy of it that cannot go stale.

What they do NOT run, stated rather than implied: the postMessage hop itself. That
needs two frames on a real origin, and `--dump-dom` returns the top document only.
Its two ends are pinned separately -- the sender's target origin and the receiver's
origin check are both asserted from source below, and the receiver's routing is
covered by `moduleForFile`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_graph_command import _chrome_binary, _dump_dom, _grab

from contextlake.kb import visualize as viz
from contextlake.kb.model import Node
from contextlake.kb.store.sqlite_store import SqliteStore

DASHBOARD_JS = (Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb"
                / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """`dashboard.js` without comments -- the comments here describe the very
    mistakes being guarded against, so a raw substring check matches the warning
    and passes while the code does the wrong thing."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


def _js_function(src: str, name: str) -> str:
    """One named function's source, by brace balance from its declaration.

    Extracted rather than transcribed: a copy of the function in this file would
    keep passing after the shipped one changed, which is the whole failure mode a
    behaviour test is supposed to close.
    """
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


# --- the map the graph page reads ----------------------------------------------


def _two_repo_store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    for repo in ("aaa/first", "zzz/last"):
        s.upsert_nodes(repo, [Node(id=f"{repo}::run", repo=repo, kind="function",
                                   name="run", file="src/run.py", line=3)])
    return s


def test_every_exported_page_carries_the_whole_wiki_map(tmp_path):
    """The map has to be complete before the first page is written, not while.

    `wiki_pages` used to be filled inside the same loop that wrote the pages, so a
    page saw only the repos written before it. Here only the LAST repo has a wiki,
    which is the case that fails: the first repo's page is written first, and under
    the old order its map was still empty. The overview is worse -- it was written
    before the loop began, so its map was empty for every repo.
    """
    s = _two_repo_store(tmp_path)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / (viz.repo_slug("zzz/last") + ".md")).write_text("# last\n\n## Overview\n\nx\n",
                                                            encoding="utf-8")
    try:
        out = viz.build_site(s, tmp_path / "site")
    finally:
        s.close()

    href = f"wiki-{viz.repo_slug('zzz/last')}.html"
    for page in ("overview.html", f"repo-{viz.repo_slug('aaa/first')}.html"):
        body = (out / page).read_text(encoding="utf-8")
        assert f'"{href}"' in body, f"{page} does not know zzz/last has a wiki"
    # and the page it points at was written
    assert (out / href).exists()
    # a repo with no wiki is not in the map, so its nodes get no dead affordance
    first = (out / f"repo-{viz.repo_slug('aaa/first')}.html").read_text(encoding="utf-8")
    assert f"wiki-{viz.repo_slug('aaa/first')}.html" not in first


def test_the_live_graph_route_says_which_repos_have_a_wiki(tmp_path):
    """Served live there is no standalone page to link, so the KEY carries the fact
    and the value is empty. Without this the embedded control could only be offered
    blind, and would land on an empty wiki tab for every repo that has none."""
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "kb.sqlite")
    for repo in ("aaa/first", "zzz/last"):
        s.upsert_nodes(repo, [Node(id=f"{repo}::run", repo=repo, kind="function",
                                   name="run", file="src/run.py", line=3)])
    (store_dir / "wiki").mkdir()
    (store_dir / "wiki" / (viz.repo_slug("zzz/last") + ".md")).write_text(
        "# last\n", encoding="utf-8")

    import socket
    import threading
    import urllib.request

    from contextlake.kb.dashboard.server import build_dashboard_server

    with socket.socket() as probe:          # a port to CONFIGURE, not just to bind:
        probe.bind(("127.0.0.1", 0))        # the server pins Host to host:port, so
        port = probe.getsockname()[1]       # port=0 makes every request a 403.
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/graph/repo-" + viz.repo_slug("aaa/first"),
            timeout=30).read().decode("utf-8")
    finally:
        srv.shutdown()
        s.close()

    m = re.search(r'"wiki":\s*(\{[^}]*\})', page)
    assert m, "the live repo page carries no wiki map"
    assert "zzz/last" in m.group(1)
    assert "aaa/first" not in m.group(1)


# --- the affordance, in a real browser -----------------------------------------

_WIKI_HARNESS = """
<script>
(function(){
  function out(id, txt){
    var d = document.createElement("div"); d.id = id; d.textContent = txt;
    document.body.appendChild(d);
  }
  function probe(nodeId, tag){
    cy.getElementById(nodeId).emit("tap");
    var a = document.querySelector("#info .wikibtn");
    out("wiki-" + tag, a ? (a.tagName + "|" + (a.getAttribute("href") || "")) : "absent");
  }
  setTimeout(function(){
    probe(__A__, "has");
    probe(__B__, "hasnt");
  }, 500);
})();
</script>
</body>"""


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
@pytest.mark.parametrize("page_of,a,b", [
    ("repo", "zzz/last::run", "zzz/last::helper"),
    ("overview", "zzz/last", "aaa/first"),
])
def test_a_node_offers_its_repo_wiki_only_when_one_exists(tmp_path, page_of, a, b):
    """Driven through the shipped `showInfo`, by tapping a node on a page `build_site`
    actually wrote. The inspector's markup is built at runtime, so none of this is
    visible in the exported HTML.

    Both page kinds, because they carry different nodes and reach `showInfo` by
    different routes: a repo page's nodes are symbols, an overview's are repos, and
    the overview additionally clusters. A synthetic payload passed the repo case and
    told nothing about the overview.

    On the repo page both nodes belong to the SAME repo, so the second probe is the
    control for a different thing than on the overview -- there it is a second repo
    with no wiki. Read the parametrize ids, not just the assertions.
    """
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "kb.sqlite")
    for repo in ("aaa/first", "zzz/last"):
        s.upsert_nodes(repo, [
            Node(id=f"{repo}::run", repo=repo, kind="function", name="run",
                 file="src/run.py", line=3),
            Node(id=f"{repo}::helper", repo=repo, kind="function", name="helper",
                 file="src/util.py", line=9),
        ])
    (store_dir / "wiki").mkdir()
    (store_dir / "wiki" / (viz.repo_slug("zzz/last") + ".md")).write_text(
        "# last\n\n## Overview\n\nx\n", encoding="utf-8")
    try:
        out = viz.build_site(s, tmp_path / "site")
    finally:
        s.close()

    src = "overview.html" if page_of == "overview" else \
        f"repo-{viz.repo_slug('zzz/last')}.html"
    harness = (_WIKI_HARNESS.replace("__A__", f'"{a}"').replace("__B__", f'"{b}"'))
    page = out / "driven.html"
    page.write_text((out / src).read_text(encoding="utf-8").replace("</body>", harness),
                    encoding="utf-8")
    dom = _dump_dom(_chrome_binary(), page, tmp_path / "profile")

    href = f"wiki-{viz.repo_slug('zzz/last')}.html"
    # standalone (not embedded): a real link to the page sitting beside this one
    assert _grab(dom, "wiki-has") == f"A|{href}"
    if page_of == "overview":
        # a repo with no generated wiki gets no control at all
        assert _grab(dom, "wiki-hasnt") == "absent"
    else:
        # same repo, so the sibling symbol gets the same link -- the affordance is
        # per REPO, and asserting "absent" here would be asserting the wrong rule
        assert _grab(dom, "wiki-hasnt") == f"A|{href}"


# --- resolving a file to a subsystem page --------------------------------------

_MODULE_HARNESS_TMPL = """
<script>
__FN__
(function(){
  function out(id, txt){
    var d = document.createElement("div"); d.id = id; d.textContent = txt;
    document.body.appendChild(d);
  }
  var mods = [{prefix: "src"}, {prefix: "src/parser"}, {prefix: "tests"}];
  out("m-longest", String(moduleForFile(mods, "src/parser/chunk.py")));
  out("m-shallow", String(moduleForFile(mods, "src/main.py")));
  out("m-sibling", String(moduleForFile(mods, "srcutil/helper.py")));
  out("m-exact", String(moduleForFile(mods, "tests")));
  out("m-nomatch", String(moduleForFile(mods, "docs/index.md")));
  out("m-nofile", String(moduleForFile(mods, null)));
})();
</script>
</body></html>"""


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_a_file_resolves_to_the_narrowest_subsystem_that_contains_it(tmp_path):
    """`src` must not claim `srcutil/helper.py`.

    An unanchored prefix test on a path is a defect this codebase has shipped
    before, and it is invisible: the reader lands on a plausible page for the
    wrong subsystem. `srcutil/helper.py` is the case that catches it, and it only
    catches it because "srcutil" is NOT one of the modules -- a bare `startsWith`
    hands it to "src", while the right answer is that nothing covers it. With
    "srcutil" in the list too, every assertion here passes either way.
    """
    fn = _js_function(DASHBOARD_JS, "moduleForFile")
    page = tmp_path / "mod.html"
    page.write_text("<!DOCTYPE html><html><body>"
                    + _MODULE_HARNESS_TMPL.replace("__FN__", fn), encoding="utf-8")
    dom = _dump_dom(_chrome_binary(), page, tmp_path / "profile")

    assert _grab(dom, "m-longest") == "src/parser"   # narrowest wins over "src"
    assert _grab(dom, "m-shallow") == "src"
    assert _grab(dom, "m-sibling") == "null"         # NOT "src"
    assert _grab(dom, "m-exact") == "tests"          # the module's own path
    assert _grab(dom, "m-nomatch") == "null"
    assert _grab(dom, "m-nofile") == "null"


# --- the payload: a file lands on its subsystem page ----------------------------

@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
@pytest.mark.parametrize("file_q,selected,shown,hidden", [
    ("&file=src/parser/f1.py", "src", "SRC-SUBSYSTEM-PAGE", "WHOLE-REPO-PAGE"),
    ("&file=docs/readme.md", None, "WHOLE-REPO-PAGE", "SRC-SUBSYSTEM-PAGE"),
    ("", None, "WHOLE-REPO-PAGE", "SRC-SUBSYSTEM-PAGE"),
])
def test_a_files_route_opens_the_subsystem_page_that_covers_it(
        tmp_path, file_q, selected, shown, hidden):
    """The whole point of V3, driven through the real dashboard.

    `?file=` is what the graph frame sends. It has to reach `renderWikiTab`, resolve
    against the subsystem list, mark the picker AND load that page. Three of those
    four could be right while the fourth silently left the reader on the repo
    overview, so this asserts the rendered TEXT as well as the dropdown: a marked
    control over unchanged content is the failure that looks like success. Each row
    also names the page that must NOT be showing, because "the right page is here"
    and "the wrong page is not" are different claims when both could render.

    The `docs/` row is the honest-degrade case. `docs` is not a subsystem with a
    page, so there is nothing to land on and the tab shows the whole-repo page --
    not an empty pane, and not some other module's page.

    Read from the dumped DOM with no injected harness: this shell is generated per
    request, so there is no file to inject into, and by dump time the SPA has
    rendered. That is also why the picker marks its option with an ATTRIBUTE -- a
    `select.value` assignment leaves nothing in the document to read.
    """
    import socket
    import subprocess
    import threading

    from contextlake.kb.cmds.wiki import _module_wiki_filename
    from contextlake.kb.dashboard.server import build_dashboard_server

    repo = "zzz/last"
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "kb.sqlite")
    # min_nodes is 5 per module, so a thin fixture offers NO modules, the picker
    # never renders, and every assertion here would pass against nothing.
    nodes = []
    for i in range(6):
        nodes.append(Node(id=f"{repo}::s{i}", repo=repo, kind="function", name=f"s{i}",
                          file=f"src/parser/f{i}.py", line=i + 1))
        nodes.append(Node(id=f"{repo}::t{i}", repo=repo, kind="function", name=f"t{i}",
                          file=f"lib/f{i}.py", line=i + 1))
    s.upsert_nodes(repo, nodes)

    wiki = store_dir / "wiki"
    (wiki / "_modules").mkdir(parents=True)
    (wiki / (viz.repo_slug(repo) + ".md")).write_text(
        "# last\n\n## Overview\n\nWHOLE-REPO-PAGE\n", encoding="utf-8")
    (wiki / "_modules" / _module_wiki_filename(repo, "src")).write_text(
        "# last - src\n\n## Overview\n\nSRC-SUBSYSTEM-PAGE\n", encoding="utf-8")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proc = subprocess.run(
            [_chrome_binary(), "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", f"--user-data-dir={tmp_path / 'profile'}",
             "--virtual-time-budget=30000", "--dump-dom",
             f"http://127.0.0.1:{port}/#/repo/{repo.replace('/', '%2F')}"
             f"?tab=wiki{file_q}"],
            capture_output=True, text=True, timeout=300)
        dom = proc.stdout
    finally:
        srv.shutdown()
        s.close()

    # the picker rendered at all -- without this the rest is vacuous
    assert 'class="cl-select"' in dom, "no subsystem picker; the fixture offered no modules"
    marked = re.findall(r'<option value="([^"]*)"[^>]*\bselected\b', dom)
    assert marked == ([selected] if selected else []), \
        f"picker marked {marked}, expected {selected}"
    assert shown in dom
    assert hidden not in dom


# --- the same graph, opened top-level ------------------------------------------


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_the_fullscreen_graph_still_offers_the_wiki(tmp_path):
    """`viewArch` ships a Fullscreen link that opens THIS page top-level.

    Same node, same graph, one click apart -- and no parent to post to. The first
    version of this feature gave the live map empty values, on the reasoning that a
    served page has nothing to link; the control then vanished here with no error,
    which is the failure this whole file is built to catch elsewhere. The fix is that
    the map carries a real dashboard route, so the link works and still narrows to
    the file's subsystem.

    Driven top-level on purpose: every other browser test here is embedded or
    exported, and neither reaches this branch.
    """
    import socket
    import subprocess
    import threading

    from contextlake.kb.dashboard.server import build_dashboard_server

    repo = "zzz/last"
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "kb.sqlite")
    s.upsert_nodes(repo, [
        Node(id=f"{repo}::run", repo=repo, kind="function", name="run",
             file="src/parser/chunk.py", line=3),
        Node(id=f"{repo}::helper", repo=repo, kind="function", name="helper",
             file="src/util.py", line=9),
    ])
    (store_dir / "wiki").mkdir()
    (store_dir / "wiki" / (viz.repo_slug(repo) + ".md")).write_text("# last\n",
                                                                    encoding="utf-8")

    import urllib.request

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = build_dashboard_server(s, store_dir, host="127.0.0.1", port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        served = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/graph/repo-{viz.repo_slug(repo)}",
            timeout=30).read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
        s.close()

    # Re-served from a plain directory rather than driven in place, because the
    # harness has to be injected and this page is generated per request. A live page
    # is `assets="inline"`, so it is self-contained; its only outbound call is
    # /neighbors, which `activateNode` makes AFTER showInfo has already rendered the
    # inspector, so a 404 there cannot hide what is being asserted. Still http, not
    # file://, so the origin is real -- exactly as the Fullscreen link opens it.
    site = tmp_path / "fullscreen"
    site.mkdir()
    (site / "page.html").write_text(
        served.replace("</body>", _CHILD_HARNESS.replace("__NODE__", f'"{repo}::run"')),
        encoding="utf-8")
    srv2, base = _serve_dir(site)
    try:
        proc = subprocess.run(
            [_chrome_binary(), "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", f"--user-data-dir={tmp_path / 'profile'}",
             "--virtual-time-budget=25000", "--dump-dom", base + "/page.html"],
            capture_output=True, text=True, timeout=300)
        dom = proc.stdout
    finally:
        srv2.shutdown()
        srv2.server_close()

    m = re.search(r'<div id="cl-probe">([^<]*)</div>', dom)
    assert m, "the inspector never rendered; nothing here is being asserted"
    # a LINK (no parent to post to), to the dashboard route, narrowed to this node's
    # own file -- the same landing the embedded button produces
    assert m.group(1) == "A|"
    assert ('href="/#/repo/zzz%2Flast?tab=wiki&amp;file=src%2Fparser%2Fchunk.py"' in dom
            or 'href="/#/repo/zzz%2Flast?tab=wiki&file=src%2Fparser%2Fchunk.py"' in dom), \
        "the fullscreen link does not carry the node's file"


# --- the frame boundary, with two real frames -----------------------------------

_CHILD_HARNESS = """
<script>
(function(){
  setTimeout(function(){
    cy.getElementById(__NODE__).emit("tap");
    var el = document.querySelector("#info .wikibtn");
    var d = document.createElement("div"); d.id = "cl-probe";
    d.textContent = el ? (el.tagName + "|" + (el.getAttribute("data-wfile") || "")) : "absent";
    document.body.appendChild(d);
    if (el && el.tagName === "BUTTON") { el.click(); }
  }, 600);
})();
</script>
</body>"""

_PARENT_PAGE = """<!DOCTYPE html><html><body>
<iframe id="f" src="__SRC__" style="width:1200px;height:800px;border:0"></iframe>
<script>
function out(id, txt){
  var d = document.createElement("div"); d.id = "p-" + id; d.textContent = txt;
  document.body.appendChild(d);
}
window.addEventListener("message", function(e){
  if (e.origin !== window.location.origin) return;
  var d = e.data;
  if (!d || d.type !== "cl-wiki") return;
  out("msg", d.repo + "|" + (d.file || ""));
});
setTimeout(function(){
  if (!document.getElementById("p-msg")) { out("msg", "none"); }
  var doc = document.getElementById("f").contentDocument;
  var probe = doc && doc.getElementById("cl-probe");
  out("child", probe ? probe.textContent : "no-probe");
}, 3000);
</script>
</body></html>
"""


def _serve_dir(directory):
    """A throwaway http server over one directory, on a real origin.

    Real http, not file://: an iframe on file:// reports origin "null", which is
    what the graph page checks for before offering the button at all, and which
    Chrome also refuses cross-frame DOM access on. The whole point of this test is
    the branch that only exists on a real origin.
    """
    import functools
    import http.server
    import socketserver
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
@pytest.mark.parametrize("repo,node,expect_control,expect_msg", [
    ("zzz/last", "zzz/last::run", "BUTTON|src/run.py", "zzz/last|src/run.py"),
    ("aaa/first", "aaa/first::run", "absent", "none"),
])
def test_an_embedded_graph_asks_its_parent_to_open_the_wiki(
        tmp_path, repo, node, expect_control, expect_msg):
    """Two frames, a real origin, and the message actually crossing.

    This is the branch the single-frame test cannot see. Embedded, the control is a
    BUTTON rather than a link, because there is no page to link to that would not
    replace the graph inside its own frame -- it asks the dashboard to route instead.
    Which means the "does this repo have a wiki" gate has NO observable effect in the
    standalone test: there the missing map entry and the missing href suppress the
    control for the same reason, so removing the gate changed nothing that was
    asserted. Here it is the only thing standing between a reader and a button that
    opens an empty wiki tab.

    Asserted at both ends: what the inspector rendered (read out of the child frame's
    own DOM) and what arrived at the parent.
    """
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "kb.sqlite")
    for r in ("aaa/first", "zzz/last"):
        s.upsert_nodes(r, [
            Node(id=f"{r}::run", repo=r, kind="function", name="run",
                 file="src/run.py", line=3),
            Node(id=f"{r}::helper", repo=r, kind="function", name="helper",
                 file="src/util.py", line=9),
        ])
    (store_dir / "wiki").mkdir()
    (store_dir / "wiki" / (viz.repo_slug("zzz/last") + ".md")).write_text(
        "# last\n", encoding="utf-8")
    try:
        out = viz.build_site(s, tmp_path / "site")
    finally:
        s.close()

    child = f"repo-{viz.repo_slug(repo)}.html"
    (out / child).write_text(
        (out / child).read_text(encoding="utf-8").replace(
            "</body>", _CHILD_HARNESS.replace("__NODE__", f'"{node}"')),
        encoding="utf-8")
    (out / "parent.html").write_text(_PARENT_PAGE.replace("__SRC__", child),
                                     encoding="utf-8")

    srv, base = _serve_dir(out)
    try:
        import subprocess
        proc = subprocess.run(
            [_chrome_binary(), "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", f"--user-data-dir={tmp_path / 'profile'}",
             "--virtual-time-budget=25000", "--dump-dom", base + "/parent.html"],
            capture_output=True, text=True, timeout=300)
        dom = proc.stdout
    finally:
        srv.shutdown()
        srv.server_close()   # the listening socket outlives shutdown()

    assert _grab(dom, "p-child") == expect_control
    assert _grab(dom, "p-msg") == expect_msg


# --- the frame boundary ---------------------------------------------------------


def test_the_frame_message_is_pinned_to_an_exact_origin():
    """The graph runs in an iframe and its message carries a repo id straight into a
    route, so the origin check is the whole boundary. Asserted from source: proving
    it needs two documents on a real origin, and `--dump-dom` gives one.

    Both halves are checked. A sender that posts to "*" hands the repo id to any
    embedder; a receiver that accepts on a substring (`indexOf`, `startsWith`) lets
    `https://example.com.evil.test` through.
    """
    js = _strip_comments(DASHBOARD_JS)
    m = re.search(r'addEventListener\("message",\s*function\s*\(e\)\s*\{(.{0,400})',
                  js, flags=re.S)
    assert m, "dashboard.js registers no message listener"
    body = m.group(1)
    assert "e.origin !== window.location.origin" in body, \
        "the listener does not compare the origin exactly"
    assert "indexOf" not in body and "startsWith" not in body, \
        "an origin checked by substring is not checked"

    app = _strip_comments(
        (Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb" / "static"
         / "app.js").read_text(encoding="utf-8"))
    send = re.search(r'window\.parent\.postMessage\((.{0,300}?)\);', app, flags=re.S)
    assert send, "app.js never posts to its parent"
    assert "window.location.origin" in send.group(1), \
        "the graph page posts to a wildcard target origin"
    assert '"*"' not in send.group(1)
