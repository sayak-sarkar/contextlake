"""F6: a repo-pair row in the dashboard drives the embedded graph's camera.

Hover or focus on a row previews the pair (the peek ring: no camera move, no change to the
selection); a click frames the pair and the edges between them without opening the
inspector, so keyboard focus stays in the dashboard. The frame accepts a closed vocabulary
(`cl-peek`, `cl-focus`), at most two string ids, and only from its own origin. A page opened
from file:// has origin "null", which names nobody, so linking is off there.

The receiver runs in a real browser, served over HTTP so the page has a real origin. The
postMessage hop between two frames is not driven (`--dump-dom` returns the top document
only); the sender's target origin and its overview-only condition are pinned from source.
"""

from __future__ import annotations

import functools
import http.server
import re
import subprocess
import threading
from pathlib import Path

import pytest
from test_graph_command import _chrome_binary, _grab

from contextlake.kb import visualize as viz

SRC = Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb"
DASHBOARD_JS = (SRC / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")
APP_JS = (SRC / "static" / "app.js").read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


_HARNESS = """
<script>
(function(){
  function out(id, txt){
    var d = document.createElement("div"); d.id = id; d.textContent = txt;
    document.body.appendChild(d);
  }
  function cam(){ var p = cy.pan(); return [Math.round(p.x), Math.round(p.y),
                                            cy.zoom().toFixed(4)].join(","); }
  function send(data, origin){
    window.dispatchEvent(new MessageEvent("message",
      {data: data, origin: origin === undefined ? location.origin : origin}));
  }
  function settle(done){
    var last = null, stable = 0;
    (function tick(){
      var now = cam(); stable = (now === last) ? stable + 1 : 0; last = now;
      if (stable >= 3) { done(); return; }
      setTimeout(tick, 60);
    })();
  }
  function hiIds(){ return cy.elements(".hi").map(function(x){ return x.id(); }).sort().join(","); }
  setTimeout(function(){ settle(function(){
    out("origin", location.origin);
    out("reduced-motion", String(window.matchMedia("(prefers-reduced-motion: reduce)").matches));
    var before = cam();
    send({type: "cl-peek", ids: ["acme/api", "acme/web"]});
    out("peek-count", String(cy.nodes(".peek").length));
    out("peek-camera-moved", cam() === before ? "no" : "YES");
    send({type: "cl-peek", ids: []});
    out("peek-cleared", String(cy.nodes(".peek").length));

    send({type: "cl-focus", ids: ["acme/api"]}, "https://evil.example");
    out("foreign-origin-hi", hiIds() || "none");
    send({type: "cl-focus", ids: ["acme/api", "acme/web", "other/api"]});
    out("three-ids-hi", hiIds() || "none");
    send({type: "cl-focus", ids: [{"id": "acme/api"}]});
    out("object-id-hi", hiIds() || "none");
    send({type: "cl-other", ids: ["acme/api"]});
    out("unknown-type-hi", hiIds() || "none");

    var activeBefore = document.activeElement;
    send({type: "cl-focus", ids: ["acme/api", "acme/web"]});
    out("focus-hi", hiIds() || "none");
    out("focus-hi-nodes", cy.nodes(".hi").map(function(x){ return x.id(); }).sort().join(","));
    out("focus-hi-edges", String(cy.edges(".hi").length));
    out("focus-kept", document.activeElement === activeBefore ? "yes" : "NO");
    // frameOn waits 210 ms before it animates, so start watching after that.
    setTimeout(function(){
      settle(function(){ out("focus-camera-moved", cam() === before ? "no" : "yes"); });
    }, 400);
  }); }, 400);
})();
</script>
</body>"""


def _graph_page(tmp_path: Path) -> Path:
    nodes = [{"id": r, "repo": r, "kind": "repo", "name": r.split("/")[-1], "deg": 1}
             for r in ("acme/api", "acme/web", "other/api", "other/db", "third/x")]
    edges = [{"id": "e1", "src": "acme/api", "dst": "acme/web", "relation": "depends_on",
              "confidence": "INFERRED", "weight": 1.0},
             {"id": "e2", "src": "other/api", "dst": "other/db", "relation": "depends_on",
              "confidence": "INFERRED", "weight": 1.0}]
    page = tmp_path / "graph.html"
    page.write_text(viz.to_html(viz.to_payload(nodes, edges, {"mode": "overview"}))
                    .replace("</body>", _HARNESS), encoding="utf-8")
    return page


def _dump(chrome: str, url: str, profile: Path, *extra: str) -> str:
    proc = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
         f"--user-data-dir={profile}", "--virtual-time-budget=20000", *extra, "--dump-dom", url],
        capture_output=True, text=True, timeout=300)
    return proc.stdout


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_the_graph_page_follows_a_same_origin_row(tmp_path):
    page = _graph_page(tmp_path)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # Reduced motion makes the framing instant. Without it the move is an animation
        # driven by requestAnimationFrame, which does not fire under --dump-dom on every
        # machine, so a "camera moved" check would pass on CI and fail here, or the reverse.
        dom = _dump(_chrome_binary(), f"http://127.0.0.1:{server.server_port}/{page.name}",
                    tmp_path / "profile", "--force-prefers-reduced-motion")
    finally:
        server.shutdown()
        server.server_close()

    assert _grab(dom, "origin").startswith("http://127.0.0.1:")
    assert _grab(dom, "reduced-motion") == "true", "the flag did not take effect"
    # Preview: the ring on both, and no camera move.
    assert _grab(dom, "peek-count") == "2"
    assert _grab(dom, "peek-camera-moved") == "no"
    assert _grab(dom, "peek-cleared") == "0"
    # Refused: a foreign origin, more than two ids, a non-string id, an unknown type.
    for probe in ("foreign-origin-hi", "three-ids-hi", "object-id-hi", "unknown-type-hi"):
        assert _grab(dom, probe) == "none", probe
    # Commit: the pair and the edge between them, framed, with keyboard focus untouched.
    assert _grab(dom, "focus-hi-nodes") == "acme/api,acme/web"
    assert _grab(dom, "focus-hi-edges") == "1"
    assert _grab(dom, "focus-kept") == "yes"
    assert _grab(dom, "focus-camera-moved") == "yes"


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_a_file_page_ignores_every_linking_message(tmp_path):
    """A file:// origin names nobody: "null" in most browsers, "file://" in Chrome. Even a
    message carrying the page's own reported origin is refused there."""
    page = _graph_page(tmp_path)
    dom = _dump(_chrome_binary(), page.as_uri(), tmp_path / "profile")
    assert _grab(dom, "origin") in ("null", "file://")
    assert _grab(dom, "peek-count") == "0"
    assert _grab(dom, "focus-hi") == "none"


def test_the_frame_checks_the_origin_and_the_vocabulary():
    src = _strip_comments(APP_JS)
    body = src[src.index('d.type !== "cl-peek" && d.type !== "cl-focus"'):][:600]
    assert '!/^https?:$/.test(location.protocol) || e.origin !== location.origin' in body
    assert "ids.length > 2" in body


def test_the_dashboard_sends_to_its_own_origin_only():
    src = _strip_comments(DASHBOARD_JS)
    fn = src[src.index("function graphLinkButton("):][:900]
    assert "postMessage({ type: type, ids: list }, window.location.origin)" in fn
    assert '"*"' not in fn
    sent = set(re.findall(r'send\("([\w-]+)"', fn))
    assert sent == {"cl-peek", "cl-focus"}


def test_the_dashboard_links_rows_only_at_overview_scope_and_not_from_file():
    src = _strip_comments(DASHBOARD_JS)
    assert 'var linkable = !id && /^https?:$/.test(window.location.protocol);' in src
    assert "linkable ? graphLinkButton(e.src, e.dst) : null" in src
