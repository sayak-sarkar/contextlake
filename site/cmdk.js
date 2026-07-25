/* Shared ⌘K command palette (docs + landing). Drop-in: a page needs only a trigger with
   id="cmdk-open", <link rel=stylesheet href=cmdk.css>, and this script. The modal is injected
   here. Typeahead over a section-level search-index.json that is semantically enriched at build
   time (nearest-neighbour "related" links) -> neighbour boost; no runtime model, fully offline.
   Combobox/listbox a11y (aria-activedescendant), focus trap + restore. ⌘K / Ctrl-K / "/" open. */
(function () {
  var CHEV = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
  // a history glyph (clock w/ rewind hand) so a recent query reads as history, not a result row
  var CLOCK = '<svg class="rec-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>';
  if (!document.getElementById("cmdk")) {
    var host = document.createElement("div");
    host.innerHTML =
      '<div class="cmdk" id="cmdk" hidden><div class="cmdk-backdrop" data-cmdk-close></div>' +
      '<div class="cmdk-panel" role="dialog" aria-modal="true" aria-label="Search the docs">' +
      '<div class="cmdk-input-row"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>' +
      '<input id="cmdk-input" class="cmdk-input" type="text" role="combobox" aria-expanded="true" aria-controls="cmdk-list" aria-autocomplete="list" aria-label="Search the docs" placeholder="Search the docs" autocomplete="off" spellcheck="false">' +
      '<button class="cmdk-esc" type="button" data-cmdk-close aria-label="Close search">esc</button></div>' +
      '<div id="cmdk-list" class="cmdk-list" role="listbox" aria-label="Search results"></div>' +
      '<div class="cmdk-foot" aria-hidden="true"><span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>' +
      '<span><kbd>↵</kbd> open</span><span class="brand"><span class="dot"></span> semantic index, offline</span></div></div></div>';
    document.body.appendChild(host.firstChild);
  }
  var trigger = document.getElementById("cmdk-open"),
    modal = document.getElementById("cmdk"),
    input = document.getElementById("cmdk-input"),
    list = document.getElementById("cmdk-list");
  if (!modal || !input || !list) return;
  var idx = null, loading = false, active = -1, lastFocus = null;
  var mac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || "");
  var hint = document.getElementById("cmdk-hint");
  if (hint) hint.textContent = mac ? "⌘K" : "Ctrl K";
  // some pages (docs) load search-index.json from the site root; resolve relative to this script
  var INDEX = (document.currentScript && document.currentScript.src
    ? document.currentScript.src.replace(/[^/]*$/, "") : "") + "search-index.json";

  function load(cb) {
    if (idx) { cb(); return; }
    if (loading) return; loading = true;
    fetch(INDEX).then(function (r) { return r.json(); }).then(function (d) { idx = d; loading = false; cb(); })
      .catch(function () { idx = []; loading = false; cb(); });
  }
  function esc(s) { return (s || "").replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function rx(t) { return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  function hl(text, toks) { var e = esc(text); if (!toks.length) return e; try { return e.replace(new RegExp("(" + toks.map(rx).join("|") + ")", "ig"), "<mark>$1</mark>"); } catch (_) { return e; } }
  function recents() { try { return JSON.parse(localStorage.getItem("cl:recent") || "[]"); } catch (e) { return []; } }
  function remember(q) { try { var r = recents().filter(function (x) { return x !== q; }); r.unshift(q); localStorage.setItem("cl:recent", JSON.stringify(r.slice(0, 6))); } catch (e) { } }
  function score(e, q, toks) {
    var t = e.title.toLowerCase(), x = (e.text || "").toLowerCase(), s = 0, hit = false;
    if (t.indexOf(q) >= 0) { s += 100; hit = true; if (t.indexOf(q) === 0) s += 50; }
    for (var i = 0; i < toks.length; i++) { var k = toks[i]; if (t.indexOf(k) >= 0) { s += 28; hit = true; } else if (x.indexOf(k) >= 0) { s += 8; hit = true; } }
    if (q.length > 2 && x.indexOf(q) >= 0) { s += 12; hit = true; }
    if (e.kind === "page") s += 6;
    return hit ? s : 0;
  }
  function fuzzy(t, q) { var p = 0; for (var i = 0; i < q.length; i++) { p = t.indexOf(q[i], p); if (p < 0) return false; p++; } return true; }
  function query(q) {
    var toks = q.split(/\s+/).filter(Boolean), scored = [], byUrl = {}, i, j;
    for (i = 0; i < idx.length; i++) { byUrl[idx[i].url] = i; var sc = score(idx[i], q, toks); if (sc > 0) scored.push({ e: idx[i], s: sc }); }
    var boost = {};
    scored.forEach(function (h) { (h.e.related || []).forEach(function (u) { boost[u] = Math.max(boost[u] || 0, h.s * 0.3); }); });
    Object.keys(boost).forEach(function (u) { var f = null; for (j = 0; j < scored.length; j++) { if (scored[j].e.url === u) { f = scored[j]; break; } } if (f) f.s += boost[u]; else if (byUrl[u] != null) scored.push({ e: idx[byUrl[u]], s: boost[u] }); });
    if (!scored.length && q.length >= 3) { for (i = 0; i < idx.length; i++) { if (fuzzy(idx[i].title.toLowerCase(), q)) scored.push({ e: idx[i], s: 5 }); } }
    scored.sort(function (a, b) { return b.s - a.s; });
    return scored.slice(0, 10).map(function (h) { return h.e; });
  }
  function optHTML(e, toks, i) {
    var crumb = e.kind === "section"
      ? '<span class="g">' + esc(e.page) + "</span>" + CHEV + "<span>" + esc(e.group || "") + "</span>"
      : '<span class="g">' + esc(e.group || "") + "</span>";
    return '<a class="cmdk-opt" role="option" id="cmdk-opt-' + i + '" href="' + e.url + '" aria-selected="false"><span class="crumb">' + crumb + '</span><span class="ttl">' + hl(e.title, toks) + "</span>" + (e.text ? '<span class="snip">' + hl(e.text.slice(0, 160), toks) + "</span>" : "") + '<span class="arrow" aria-hidden="true">↵</span></a>';
  }
  function opts() { return list.querySelectorAll(".cmdk-opt"); }
  function setActive(n) {
    var o = opts(); if (!o.length) { active = -1; input.removeAttribute("aria-activedescendant"); return; }
    active = (n + o.length) % o.length;
    for (var i = 0; i < o.length; i++) o[i].setAttribute("aria-selected", i === active ? "true" : "false");
    input.setAttribute("aria-activedescendant", o[active].id); o[active].scrollIntoView({ block: "nearest" });
  }
  function render() {
    var q = input.value.trim();
    if (!q) { empty(); return; }
    var items = query(q), toks = q.toLowerCase().split(/\s+/).filter(Boolean);
    if (!items.length) { list.innerHTML = '<div class="cmdk-empty">No matches for &ldquo;' + esc(q) + '&rdquo;<span class="hint">Try a feature: graph, embed, wiki, serve, or owners.</span></div>'; active = -1; input.removeAttribute("aria-activedescendant"); return; }
    list.innerHTML = items.map(function (e, i) { return optHTML(e, toks, i); }).join(""); setActive(0);
  }
  function empty() {
    if (!idx) { list.innerHTML = '<div class="cmdk-empty">Loading the index…</div>'; return; }
    var r = recents(), html = "", pages = idx.filter(function (e) { return e.kind === "page"; }).slice(0, 6);
    if (r.length) html += '<div class="cmdk-sec-label">Recent</div>' + r.map(function (q) { return '<button class="cmdk-opt cmdk-recent" type="button" role="option" data-q="' + esc(q) + '" aria-selected="false" aria-label="Search again for ' + esc(q) + '">' + CLOCK + '<span class="rec-q">' + esc(q) + '</span><span class="arrow" aria-hidden="true">↵</span></button>'; }).join("");
    html += '<div class="cmdk-sec-label">Jump to a page</div>' + pages.map(function (e, i) { return optHTML(e, [], i); }).join("");
    list.innerHTML = html;
    var o = opts(); for (var i = 0; i < o.length; i++) o[i].id = "cmdk-opt-" + i;
    setActive(0);
  }
  function activate(el) {
    if (!el) return;
    if (el.dataset.q) { input.value = el.dataset.q; render(); input.focus(); return; }
    var q = input.value.trim(); if (q) remember(q);
    window.location.href = el.getAttribute("href");
  }
  function open() {
    if (!modal.hidden) return;
    lastFocus = document.activeElement; modal.hidden = false;
    document.documentElement.style.overflow = "hidden"; input.value = "";
    load(function () { render(); }); render();
    setTimeout(function () { input.focus(); }, 0);
  }
  function close() {
    if (modal.hidden) return;
    modal.hidden = true; document.documentElement.style.overflow = "";
    input.removeAttribute("aria-activedescendant");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  if (trigger) trigger.addEventListener("click", open);
  document.addEventListener("keydown", function (e) {
    if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) { e.preventDefault(); modal.hidden ? open() : close(); return; }
    if (e.key === "/" && modal.hidden) { var a = document.activeElement, tag = a && a.tagName; if (tag !== "INPUT" && tag !== "TEXTAREA" && !(a && a.isContentEditable)) { e.preventDefault(); open(); } return; }
    if (modal.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
    else if (e.key === "Enter") { e.preventDefault(); activate(opts()[active]); }
    else if (e.key === "Tab") { e.preventDefault(); }
  });
  input.addEventListener("input", render);
  list.addEventListener("click", function (e) { var el = e.target.closest(".cmdk-opt"); if (!el) return; if (el.dataset.q) { e.preventDefault(); activate(el); } else { var q = input.value.trim(); if (q) remember(q); } });
  list.addEventListener("mousemove", function (e) { var el = e.target.closest(".cmdk-opt"); if (!el) return; var o = opts(); for (var i = 0; i < o.length; i++) if (o[i] === el) { setActive(i); break; } });
  modal.addEventListener("mousedown", function (e) { if (e.target === modal || e.target.hasAttribute("data-cmdk-close")) close(); });
})();
