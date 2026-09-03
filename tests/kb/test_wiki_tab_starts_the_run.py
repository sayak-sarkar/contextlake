"""The Wiki and Docs tabs start the real run instead of copying a command.

A control labelled "Generate wiki" that writes text to the clipboard reports
progress for work that did not happen. The real runner already existed
(``wikiRegenerateCard`` -> ``/api/wiki/generate``), gated on ``--allow-mutations``,
so with mutations on the tab used to render the decoy and the working control side
by side, and with mutations off it rendered the decoy alone.

Every assertion here is a COUNT over the dumped DOM, because "one control" and "no
decoy" are counting claims: an assertion that the right control is present says
nothing about a second one beside it.

The Docs tab's control is the same shape one tab over: with mutations it is a real
``/api/docs/generate`` runner, without them it is the clipboard command. Its rows sit
in this file rather than a sixth one because they need this file's store fixture and
its port-probing Chromium driver, and because the two tabs are one claim.

``--dump-dom`` is the right driver for the counting rows. It is not for pages that
depend on ``requestAnimationFrame`` or ``IntersectionObserver``, and ``dashboard.js``
uses neither; the shell serves ``dashboard.js`` as its own route rather than inlining
it (``server.py:163-169``), so a dumped DOM carries rendered output and not the script
source. The click-through -- the run actually starting -- is covered by a separate
browser pass, not by a DOM dump.

One row measures instead of counting. A CSS width is invisible in a dumped DOM, so
``test_a_run_log_never_widens_the_page`` replays the page this file's own driver
dumped, next to the shipped stylesheet, and reads the widths back out of a second
Chromium. See that test for why a replay is the honest driver here.
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
import urllib.parse

import pytest
from test_graph_command import _chrome_binary

from contextlake.kb import visualize as viz
from contextlake.kb.model import Node, Repo
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "acme/app"


def _nodes(repo=REPO):
    """Six nodes under src/parser and six under lib.

    ``repo_modules`` needs 5 nodes per module, so a thinner fixture offers no
    modules at all, the subsystem picker never renders, and the module-scoped
    assertions below would pass against nothing.
    """
    out = []
    for i in range(6):
        out.append(Node(id=f"{repo}::s{i}", repo=repo, kind="function", name=f"s{i}",
                        file=f"src/parser/f{i}.py", line=i + 1))
        out.append(Node(id=f"{repo}::t{i}", repo=repo, kind="function", name=f"t{i}",
                        file=f"lib/f{i}.py", line=i + 1))
    return out


def _store(tmp_path, *, whole_repo_page=False, module_page=False, documents=False):
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    s = SqliteStore(store_dir / "kb.sqlite")
    s.upsert_repo(Repo(id=REPO, path=str(tmp_path), head_commit="h1"))
    s.upsert_nodes(REPO, _nodes())
    if whole_repo_page:
        wiki = store_dir / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / (viz.repo_slug(REPO) + ".md")).write_text(
            "# app\n\n## Overview\n\nWHOLE-REPO-PAGE\n", encoding="utf-8")
    if module_page:
        from contextlake.kb.cmds.wiki import _module_wiki_filename

        mods = store_dir / "wiki" / "_modules"
        mods.mkdir(parents=True, exist_ok=True)
        (mods / _module_wiki_filename(REPO, "src")).write_text(
            "# app - src\n\n## Overview\n\nSRC-SUBSYSTEM-PAGE\n", encoding="utf-8")
    if documents:
        # Written to the two directories ``cmds.docs`` itself writes, under the slug
        # ``data._docs_out`` reads back, so ``found`` is true for the reason it is true
        # in production rather than because a flag was set.
        from contextlake.kb.cmds.docs import API_DIR, DESIGN_DIR

        for parts in (API_DIR, DESIGN_DIR):
            d = store_dir.joinpath(*parts)
            d.mkdir(parents=True, exist_ok=True)
            (d / (viz.repo_slug(REPO) + ".md")).write_text(
                "# app\n\nDOCUMENT-ON-DISK\n", encoding="utf-8")
    return s, store_dir


def _dom(tmp_path, store, store_dir, *, mutations, query="", tab="wiki"):
    """Render one of the repo's tabs in a real Chromium at a real HTTP origin.

    The port is probed and then CONFIGURED, never left at 0: the server pins the
    allowed Host header to ``host:port``, so a server built with port=0 answers 403
    to every request it then serves on its real port.
    """
    from contextlake.kb.dashboard.server import build_dashboard_server

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = build_dashboard_server(store, store_dir, host="127.0.0.1", port=port,
                                 allow_mutations=mutations)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = (f"http://127.0.0.1:{port}/#/repo/"
               f"{urllib.parse.quote(REPO, safe='')}?tab={tab}{query}")
        proc = subprocess.run(
            [_chrome_binary(), "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", f"--user-data-dir={tmp_path / 'profile'}",
             "--virtual-time-budget=30000", "--dump-dom", url],
            capture_output=True, text=True, timeout=300)
        return proc.stdout
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()


_COPY = 'title="Copy: contextlake kb wiki'


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_the_wiki_tab_offers_one_run_control_and_no_copy_button_with_mutations(tmp_path):
    """Mutations on, no wiki: one run control, zero clipboard buttons.

    Rendering the card starts nothing. Its ``poll()`` reads /api/wiki/status, which
    stats a pidfile; only a button click reaches wikiGenerate.
    """
    store, store_dir = _store(tmp_path)
    dom = _dom(tmp_path, store, store_dir, mutations=True)

    # Precondition. Without it every count below is a count of an unrendered pane.
    assert "No wiki generated for this repo" in dom

    assert dom.count(_COPY) == 0, "the clipboard decoy is still rendered"
    assert dom.count("cl-card--wikigen") == 1
    assert ">Generate wiki<" in dom
    assert "Regenerate wiki" not in dom


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_the_wiki_tab_without_mutations_copies_the_command_and_names_bootstrap(tmp_path):
    """Mutations off, no wiki: this pane has no run path at all.

    The copy button is the correct affordance here, and the sentence naming
    ``contextlake bootstrap`` is the only thing on the page that tells a reader
    where a wiki comes from when they cannot start one.
    """
    store, store_dir = _store(tmp_path)
    dom = _dom(tmp_path, store, store_dir, mutations=False)

    assert "No wiki generated for this repo" in dom

    assert dom.count(_COPY) == 1
    assert dom.count("cl-card--wikigen") == 0
    assert "contextlake bootstrap" in dom
    assert "--no-wiki" in dom


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_a_module_pane_keeps_only_the_repo_scoped_run_control_and_names_it(
        tmp_path, monkeypatch):
    """A module pane carries one run control, repo-scoped, and the sentence names it.

    The earlier name on this row said "offers no run control" while the body asserted
    one, so the name stated a claim the body contradicted. One control is the right
    answer: the card mounts on the pane, is labelled "for this repo only", and survives
    a subsystem switch. What was wrong was the sentence beside it, which told the reader
    to pick Whole repo to start a run while the run control sat directly below.

    PRECONDITION, stated because a reader cannot reach this pane today:
    ``data.repo_modules`` sets ``has_page`` from ``wiki_file.exists()`` and
    ``data.repo_wiki`` sets ``found`` from ``wiki_file.exists()`` on the same path,
    so every module the picker offers resolves. The branch is defensive, not dead --
    ``wikiContentNode`` already guards ``!w || !w.found`` and the two reads are two
    separate HTTP requests -- so this test STAGES the one condition that reaches it:
    the page is listed by /modules and absent by the time /wiki?module= is served.

    Both wiki pages are written so the picker offers "src" AND the whole-repo pane is
    not itself an empty state: an empty whole-repo pane could supply every assertion
    below on its own.
    """
    from contextlake.kb.dashboard import data as kbdata

    real = kbdata.repo_wiki

    def _missing_module_page(store, store_dir, repo_id, *, module=None, **kw):
        if module:
            return {"found": False, "stale": True, "html": None}
        return real(store, store_dir, repo_id, module=module, **kw)

    monkeypatch.setattr(kbdata, "repo_wiki", _missing_module_page)

    store, store_dir = _store(tmp_path, whole_repo_page=True, module_page=True)
    dom = _dom(tmp_path, store, store_dir, mutations=True,
               query="&file=src/parser/f1.py")

    # Preconditions. Both are required, or the rest reads a whole-repo pane.
    marked = re.findall(r'<option value="([^"]*)"[^>]*\bselected\b', dom)
    assert marked == ["src"], f"picker marked {marked}, expected src"
    assert "No wiki generated for module" in dom

    assert "generated per repo" in dom
    # The guidance half, not "Whole repo": that string is supplied by the subsystem
    # picker's own option label ("Whole repo -- overview"), which this pane renders
    # whatever the sentence says, so asserting it guarded nothing.
    assert "starts a run for the whole repo" in dom
    assert dom.count(_COPY) == 0
    # The one surviving control is the repo-scoped card, with its repo-scoped
    # label. A module control would carry neither.
    assert dom.count("cl-card--wikigen") == 1
    assert ">Regenerate wiki<" in dom


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
@pytest.mark.parametrize("mutations", [True, False])
def test_a_generated_wiki_leaves_no_empty_state_and_one_card(tmp_path, mutations):
    """A repo that HAS a page is regenerated, not generated.

    VACUITY CHECK, run by hand once: with ``whole_repo_page=False`` this row fails
    on the empty-state assertion, which proves the fixture carries the case it
    guards rather than passing because nothing rendered.
    """
    store, store_dir = _store(tmp_path, whole_repo_page=True)
    dom = _dom(tmp_path, store, store_dir, mutations=mutations)

    assert "WHOLE-REPO-PAGE" in dom, "the wiki page did not render; nothing below is asserted"
    assert re.findall(r"no wiki generated", dom, re.I) == []
    assert dom.count("cl-card--wikigen") == (1 if mutations else 0)
    if mutations:
        assert ">Regenerate wiki<" in dom
        assert ">Generate wiki<" not in dom


# ---- Docs tab (S1.2) --------------------------------------------------------
# The same three claims one tab over, counted the same way. Before these rows the
# whole user-facing half of S1.2 could be deleted -- the card's mount in
# ``renderDocsTab`` and the mutations branch in ``docsEmptyState`` -- with every test
# in the suite still passing.
_DOCS_CARD = 'data-test="docs-regenerate"'
_DOCS_COPY = 'title="Copy: contextlake kb docs'


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_the_docs_tab_offers_one_run_control_and_no_copy_button_with_mutations(tmp_path):
    """Mutations on, no documents: one run control, zero clipboard buttons.

    The heading and the button are asserted as well as the count, because the card's
    wording is what tells the reader which of the two states they are in. A repo with
    no documents is not being regenerated.
    """
    store, store_dir = _store(tmp_path)
    dom = _dom(tmp_path, store, store_dir, mutations=True, tab="docs")

    # Precondition. Without it every count below is a count of an unrendered pane.
    assert "No api reference generated for this repo yet" in dom

    assert dom.count(_DOCS_COPY) == 0, "the clipboard decoy is still rendered"
    assert dom.count(_DOCS_CARD) == 1
    assert ">Generate documents<" in dom
    assert "Regenerate documents" not in dom


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_the_docs_tab_without_mutations_copies_the_command_and_names_bootstrap(tmp_path):
    """Mutations off, no documents: this pane has no run path, so the command is right.

    ``contextlake bootstrap`` is the only thing on the page that tells a reader where
    generated documents come from when they cannot start a run from here.
    """
    store, store_dir = _store(tmp_path)
    dom = _dom(tmp_path, store, store_dir, mutations=False, tab="docs")

    assert "No api reference generated for this repo yet" in dom

    assert dom.count(_DOCS_COPY) == 1
    assert dom.count(_DOCS_CARD) == 0
    assert "contextlake bootstrap" in dom
    assert "--no-docs" in dom


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_the_docs_card_reads_regenerate_when_the_documents_are_on_disk(tmp_path):
    """Documents on disk: the card retitles, the way the wiki card next door does.

    VACUITY CHECK, run by hand once: with ``documents=False`` this row fails on
    ``DOCUMENT-ON-DISK``, which proves the fixture carries the case it guards.
    """
    store, store_dir = _store(tmp_path, documents=True)
    dom = _dom(tmp_path, store, store_dir, mutations=True, tab="docs")

    assert "DOCUMENT-ON-DISK" in dom, "the document did not render; nothing below is asserted"
    assert re.findall(r"no api reference generated", dom, re.I) == []

    assert dom.count(_DOCS_CARD) == 1
    assert ">Regenerate documents<" in dom
    assert ">Generate documents<" not in dom


# ---- Layout: a run log never widens the page --------------------------------
_LONG_LOG = ("[11:49:51] OK API reference and design notes: 1 of each written -> "
             "/a/deliberately/long/store/path/that/cannot/wrap/docs/api\n[11:49:51] done")

_MEASURE_JS = """
function clMeasure() {
  var pre = document.querySelector('.cl-card[data-test="docs-regenerate"] pre.cl-snippet');
  var main = document.querySelector('.cl-main');
  var de = document.documentElement;
  var out = { found: !!(pre && main) };
  if (out.found) {
    // What poll() does when a run reports a log: unhide the box and fill it.
    pre.hidden = false;
    pre.textContent = %s;
    out.clientW = de.clientWidth;
    out.mainW = Math.round(main.getBoundingClientRect().width);
    out.preScrollW = pre.scrollWidth;
    out.preClientW = pre.clientWidth;
  }
  var box = document.getElementById('cl-measured');
  if (!box) {
    box = document.createElement('div');
    box.id = 'cl-measured';
    document.body.appendChild(box);
  }
  box.textContent = JSON.stringify(out);
}
// On `load` only. A parse-time call can run before the stylesheet is applied, and a
// future failure reporting those numbers would read as a real layout defect. The box
// is created inside the function, so the caller's "did the script run" guard still
// catches a script that never ran at all.
window.addEventListener('load', clMeasure);
"""


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_a_run_log_never_widens_the_page(tmp_path):
    """At a 420px window the run log scrolls inside its own box, not the page.

    ``.cl-main`` is a grid item of ``.cl-shell``, and a grid item's ``min-width``
    defaults to ``auto``, which resolves to min-content. One unwrappable log line
    therefore grew the whole column past the viewport and the page scrolled sideways;
    the log box's own ``overflow-x: auto`` never engaged, because the column kept
    growing instead of clipping it. Measured before the fix at a 420px window:
    clientWidth 485, ``.cl-main`` 904.

    WHY A REPLAY. A CSS width cannot be read out of ``--dump-dom``, and this suite has
    no browser-eval driver. So this row dumps the real page with the driver above, then
    serves that dumped DOM next to the shipped stylesheet and reads the widths back out
    of a second Chromium. The DOM is the application's own rendered output and the CSS
    is the shipped file; what the replay drops is ``dashboard.js``, which is why the
    script fills the log box itself rather than waiting for a run.

    TWO numbers, not one. ``mainW == clientW`` alone would also pass if the snippet
    wrapped, which would destroy the log's fidelity. ``preScrollW > preClientW`` is what
    says the wide content is still wide and is scrolling inside its own container.
    """
    import functools
    import http.server
    import json
    import re as _re
    from pathlib import Path

    from contextlake.kb import dashboard as dashboard_pkg

    store, store_dir = _store(tmp_path)
    live_dom = _dom(tmp_path, store, store_dir, mutations=True, tab="docs")
    assert 'data-test="docs-regenerate"' in live_dom, "no run control to measure"

    replay = tmp_path / "replay"
    replay.mkdir()
    css = Path(dashboard_pkg.__file__).resolve().parent / "static" / "dashboard.css"
    (replay / "dashboard.css").write_text(css.read_text(encoding="utf-8"), encoding="utf-8")
    # Scripts out: the replay measures the DOM the application produced, and re-running
    # dashboard.js against a server that is not there would replace it with an error pane.
    page = _re.sub(r"<script\b.*?</script>", "", live_dom, flags=_re.S | _re.I)
    page += "<script>" + (_MEASURE_JS % json.dumps(_LONG_LOG)) + "</script>"
    (replay / "page.html").write_text(page, encoding="utf-8")

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(replay))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        proc = subprocess.run(
            [_chrome_binary(), "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", f"--user-data-dir={tmp_path / 'profile2'}",
             "--window-size=420,900", "--virtual-time-budget=15000", "--dump-dom",
             f"http://127.0.0.1:{port}/page.html"],
            capture_output=True, text=True, timeout=300)
    finally:
        httpd.shutdown()
        httpd.server_close()

    m = _re.search(r'<div id="cl-measured">(.*?)</div>', proc.stdout, _re.S)
    assert m, f"the measuring script did not run: {proc.stdout[-800:]}"
    got = json.loads(m.group(1))
    assert got["found"], "the replayed page carries no run log box; nothing was measured"

    assert got["mainW"] == got["clientW"], (
        f"the content column is {got['mainW']}px inside a {got['clientW']}px viewport, "
        "so the page scrolls sideways")
    assert got["preScrollW"] > got["preClientW"], (
        f"the log box does not scroll its own content ({got['preScrollW']} <= "
        f"{got['preClientW']}); the line wrapped instead")
