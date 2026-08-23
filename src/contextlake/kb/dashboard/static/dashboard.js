/* contextlake knowledge dashboard SPA — "The Sounding Line".
   Vanilla JS, one IIFE, hash router, no framework, no build step. Works in two modes
   from one artifact:
     - live   : served by `dashboard --serve`; reads /api/* via fetch, graph iframes
                point at /graph/*.
     - static : `dashboard --site` opened from file://; fetch() is blocked, so the
                snapshot is injected as a classic-script global (window.__CONTEXTLAKE__)
                and graph iframes point at sibling graph/*.html pages.
   The CL.data layer normalizes both modes to one shape; everything else is unaware. */
(function () {
  "use strict";

  // ---- mode + snapshot --------------------------------------------------
  var SNAP = window.__CONTEXTLAKE__ || null;
  var MODE = SNAP ? "static" : "live";

  var CONF = (SNAP && SNAP.confidence) || {
    EXTRACTED: ["Extracted", "#2BB3A3", "Direct from source (AST / manifest)"],
    INFERRED: ["Inferred", "#E7B53C", "Deduced (second-pass / heuristic)"],
    AMBIGUOUS: ["Ambiguous", "#e76f51", "Uncertain (flagged for review)"]
  };
  // Lock the shell's confidence colours to the snapshot's triple so the dashboard
  // and the embedded graph can never drift (mirrors visualize.CONF_META).
  try {
    var rootStyle = document.documentElement.style;
    rootStyle.setProperty("--cl-conf-extracted", CONF.EXTRACTED[1]);
    rootStyle.setProperty("--cl-conf-inferred", CONF.INFERRED[1]);
    rootStyle.setProperty("--cl-conf-ambiguous", CONF.AMBIGUOUS[1]);
  } catch (e) { /* ignore */ }

  var KIND_GLYPHS = {
    file: 1, page: 1, module: 1, class: 1, struct: 1, interface: 1, enum: 1,
    function: 1, method: 1, package: 1, repo: 1, issue: 1, design: 1, endpoint: 1, topic: 1,
    config_key: 1, test: 1
  };
  var LANG_LABELS = {
    python: "PY", javascript: "JS", typescript: "TS", tsx: "TS", csharp: "C#",
    c_sharp: "C#", java: "JV", go: "GO", ruby: "RB", rust: "RS", php: "PHP",
    kotlin: "KT", cpp: "C++", c: "C"
  };

  // ---- tiny DOM helpers (textContent-safe; data is not HTML-escaped server-side) --
  function h(tag, attrs) {
    var e = document.createElement(tag), i, k, v;
    if (attrs) {
      for (k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        v = attrs[k];
        if (v == null || v === false) continue;
        if (k === "class") e.className = v;
        else if (k === "html") e.innerHTML = v;           // fixed icon strings / server HTML only
        else if (k === "text") e.textContent = v;
        else if (k === "dataset") { for (i in v) e.dataset[i] = v[i]; }
        else if (k.slice(0, 2) === "on" && typeof v === "function") e.addEventListener(k.slice(2), v);
        else if (v === true) e.setAttribute(k, "");
        else e.setAttribute(k, v);
      }
    }
    for (i = 2; i < arguments.length; i++) append(e, arguments[i]);
    return e;
  }
  function append(parent, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { append(parent, x); }); return; }
    // Append a real DOM node as one; anything else becomes text. `instanceof Node` rather
    // than a `c.nodeType` duck-type on purpose: only the former proves the type, both to a
    // reader and to static analysis, and nothing here ever receives a node from another
    // realm -- the dashboard renders into its own document, and the graph is a separate page
    // with its own script.
    //
    // The original `typeof c === "object"` handed a plain object straight to appendChild,
    // which throws a DOMException whose message embeds the value it refused -- and that
    // message is rendered back into the error state block, which is the exception-to-render
    // round trip CodeQL traces through here.
    parent.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function icon(id, cls) {
    return '<svg class="' + (cls || "cl-ic") + '" aria-hidden="true"><use href="#' + id + '"></use></svg>';
  }
  function kindIcon(kind) {
    var k = KIND_GLYPHS[kind] ? kind : "file";
    return h("span", { class: "cl-kindglyph", html: icon("g-" + k), title: kind || "node" });
  }
  function debounce(fn, ms) {
    var t; return function () { var a = arguments, c = this;
      clearTimeout(t); t = setTimeout(function () { fn.apply(c, a); }, ms); };
  }
  function live(msg) { var r = $("#cl-live"); if (r) r.textContent = msg; }
  // Only web/mail schemes may become an href — blocks javascript:/data: XSS from
  // untrusted connector URLs (defence-in-depth; the server also allowlists schemes).
  function safeHref(u) {
    try { var p = new URL(u, location.href).protocol; return (p === "http:" || p === "https:" || p === "mailto:") ? u : null; }
    catch (e) { return null; }
  }

  // ---- localStorage (file:// can throw) ---------------------------------
  function lsGet(k, d) { try { var v = localStorage.getItem("cl:" + k); return v == null ? d : v; } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem("cl:" + k, v); } catch (e) { /* ignore */ } }

  // ---- data layer (normalizes live fetch + static global to one shape) ---
  function encPath(id) { return id.split("/").map(encodeURIComponent).join("/"); }
  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status); return r.json();
    });
  }
  // Mutating routes (sync/add-repo/MCP lifecycle) only exist when the server was
  // started with --allow-mutations; window.__CL_TOKEN__ is minted per-launch and
  // wired into this served script at build time -- never persisted, never logged.
  var MUTATIONS = !!window.__CL_MUTATIONS__;
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Contextlake-Token": window.__CL_TOKEN__ || "" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }
  var CL = {};
  CL.data = {
    overview: function () {
      return MODE === "static" ? Promise.resolve(SNAP.overview) : fetchJSON("/api/overview");
    },
    repo: function (id) {
      if (MODE === "static") {
        var d = SNAP.repos && SNAP.repos[id];
        return d ? Promise.resolve(d) : Promise.reject(new Error("repo not in snapshot"));
      }
      return fetchJSON("/api/repo/" + encPath(id));
    },
    rel: function (id) {
      if (MODE === "static") {
        var d = SNAP.relationships && SNAP.relationships[id];
        return d ? Promise.resolve(d) : Promise.reject(new Error("relationships not in snapshot"));
      }
      return fetchJSON("/api/repo/" + encPath(id) + "/rel");
    },
    // Fleet-wide equivalent of rel() above -- backs the Architecture "Overview"
    // scope's text/table equivalent of the whole-fleet graph (WCAG 1.1.1). A
    // static export built before this existed has no `fleet_relationships`
    // key; that's not an error, it's an older snapshot, so this rejects the
    // same way rel() does for an unknown id and the caller degrades gracefully
    // rather than showing an error state.
    fleetRel: function () {
      if (MODE === "static") {
        return SNAP.fleet_relationships ? Promise.resolve(SNAP.fleet_relationships)
          : Promise.reject(new Error("fleet relationships not in snapshot"));
      }
      return fetchJSON("/api/relationships");
    },
    health: function () {
      return MODE === "static" ? Promise.resolve(SNAP.health) : fetchJSON("/api/health");
    },
    // Diagrams render existing graph data through kb/visualize/ (the same text
    // `contextlake graph --format ...` produces) -- nothing to snapshot as a
    // portable static export any more than MCP/Settings are, so static rejects
    // and asyncPanel's existing live-only empty state takes over.
    diagram: function (id, fmt, module) {
      if (MODE === "static") return Promise.reject(new Error("live only"));
      var url = "/api/repo/" + encPath(id) + "/diagram?format=" + encodeURIComponent(fmt);
      if (module) url += "&module=" + encodeURIComponent(module);
      return fetchJSON(url);
    },
    // Populates the Diagrams tab's module scope-down control -- only fetched
    // when a repo's diagram comes back truncated, so small repos never pay for it.
    // `within`, when given, asks for the next level down (that module's own
    // children) instead of the top-level list -- the recursive drill-down.
    // `wikiPages`, when true, asks the server to add a `has_page` flag to each
    // module (whether wiki generation actually wrote it a subsystem page) --
    // used by the Wiki tab's module picker, not the Diagrams tab, so it's opt-in.
    repoModules: function (id, within, wikiPages) {
      if (MODE === "static") return Promise.reject(new Error("live only"));
      var url = "/api/repo/" + encPath(id) + "/modules";
      var params = [];
      if (within) params.push("within=" + encodeURIComponent(within));
      if (wikiPages) params.push("wiki=true");
      if (params.length) url += "?" + params.join("&");
      return fetchJSON(url);
    },
    // Wiki content for one repo, optionally scoped to a subsystem/module page
    // (see server-side repo_wiki()'s docstring for why this is its own route
    // rather than a ?module= param on repo() -- switching the Wiki tab's
    // module picker should only re-fetch this section, not the whole
    // repo-detail payload). The whole-repo page also arrives bundled in
    // repo()'s own `wiki` key (d.wiki) -- this is only needed for a module.
    repoWiki: function (id, module) {
      if (MODE === "static") return Promise.reject(new Error("live only"));
      var url = "/api/repo/" + encPath(id) + "/wiki";
      if (module) url += "?module=" + encodeURIComponent(module);
      return fetchJSON(url);
    },
    // The generated API reference / design notes for one repo. Its own route for the
    // same reason repoWiki is: the Docs tab switches between the two kinds and should
    // re-fetch only this section. Unlike the wiki these are NOT bundled into the
    // repo-detail payload, so the first render always fetches.
    repoDocs: function (id, kind) {
      if (MODE === "static") return Promise.reject(new Error("live only"));
      return fetchJSON("/api/repo/" + encPath(id) + "/docs?kind=" +
        encodeURIComponent(kind || "api"));
    },
    // sequencediagram needs a single symbol seed, not a whole repo, so it's served off
    // /api/impact/diagram (same family as impact() below) rather than /api/repo/.../diagram.
    sequenceDiagram: function (nodeId, hops) {
      return MODE === "static" ? Promise.reject(new Error("live only"))
        : fetchJSON("/api/impact/diagram?node=" + encodeURIComponent(nodeId) + "&hops=" + (hops || 2));
    },
    // File->table reads/writes, intra-repo only (kb/flow/data.py) -- a different row
    // shape than rel()'s repo-pair edges, so it isn't folded into repo_relationships_bulk's
    // static snapshot; live only, same precedent as diagram()/sequenceDiagram() above.
    dataFlow: function (id) {
      return MODE === "static" ? Promise.reject(new Error("live only"))
        : fetchJSON("/api/repo/" + encPath(id) + "/data-flow");
    },
    impact: function (seed, hops, limit) {
      if (MODE === "static") {
        var rec = (SNAP.impact && SNAP.impact[seed]) || null;
        if (!rec) {
          var m = (SNAP.symbols || []).filter(function (s) { return s.id === seed || s.name === seed; })[0];
          if (m) rec = (SNAP.impact && SNAP.impact[m.id]) ||
            { seed: m.id, name: m.name, found: true, hops: 3, total: 0, truncated: false, hits: [] };
        }
        if (!rec) return Promise.resolve({ seed: seed, found: false, static_missing: true, hits: [], total: 0, truncated: false });
        return Promise.resolve(rec);
      }
      return fetchJSON("/api/impact?node=" + encodeURIComponent(seed) +
        "&hops=" + (hops || 3) + "&limit=" + (limit || 100));
    },
    // "How does A reach B" -- live only (BFS over the real graph, same
    // precedent as diagram()/dataFlow()/sequenceDiagram() above).
    path: function (from, to, maxHops) {
      if (MODE === "static") return Promise.reject(new Error("live only"));
      return fetchJSON("/api/path?from=" + encodeURIComponent(from) +
        "&to=" + encodeURIComponent(to) + "&max_hops=" + (maxHops || 6));
    },
    search: function (q, kind, repo) {
      if (MODE === "static") {
        var ql = q.toLowerCase();
        var rows = (SNAP.symbols || []).filter(function (s) {
          if (kind && s.kind !== kind) return false;
          if (repo && s.repo !== repo) return false;
          return (s.name || "").toLowerCase().indexOf(ql) >= 0 ||
            (s.qualified_name || "").toLowerCase().indexOf(ql) >= 0;
        }).slice(0, 50);
        return Promise.resolve({ query: q, semantic: false, total: rows.length, results: rows });
      }
      var u = "/api/search?q=" + encodeURIComponent(q) + "&limit=50";
      if (kind) u += "&kind=" + encodeURIComponent(kind);
      if (repo) u += "&repo=" + encodeURIComponent(repo);
      return fetchJSON(u);
    },
    symbols: function () { return MODE === "static" ? (SNAP.symbols || []) : null; },
    // MCP console + Settings describe this machine/process, not the graph --
    // there is nothing meaningful to snapshot into a portable --site export,
    // so static mode rejects and asyncPanel's existing "live-only" empty
    // state (same one semantic search already uses) takes over automatically.
    mcp: function () {
      return MODE === "static" ? Promise.reject(new Error("live only")) : fetchJSON("/api/mcp");
    },
    settings: function () {
      return MODE === "static" ? Promise.reject(new Error("live only")) : fetchJSON("/api/settings");
    },
    // Chat: free graph-router answers always; LLM prose layered on top only
    // when the server was started with --llm-chat (window.__CL_LLM_CHAT__).
    // Same live-only treatment as MCP console/Settings -- there's no server
    // for a static --site export to ask.
    chat: function (question) {
      return MODE === "static" ? Promise.reject(new Error("live only"))
        : postJSON("/api/chat", { question: question });
    },
    // Mutating actions: live + --allow-mutations only. Never available in static
    // mode (there's no server to send them to) -- callers gate on MUTATIONS first.
    syncRepo: function (id) { return postJSON("/api/repo/" + encPath(id) + "/sync"); },
    addRepo: function (url) { return postJSON("/api/repo/add", { url: url }); },
    mcpServe: function (action, opts) {
      var body = Object.assign({ action: action }, opts || {});
      return postJSON("/api/mcp/serve", body);
    },
    // Live wiki (re)generation: single-repo (repoId given) or fleet-wide
    // (repoId omitted, mirrors `contextlake wiki` with no args). estimate()
    // is read-only (no LLM call) -- the count shown before the user confirms.
    wikiStatus: function () {
      return MODE === "static" ? Promise.reject(new Error("live only")) : fetchJSON("/api/wiki/status");
    },
    wikiEstimate: function (repoId, force) {
      var params = [];
      if (repoId) params.push("repo=" + encodeURIComponent(repoId));
      if (force) params.push("force=true");
      return fetchJSON("/api/wiki/estimate" + (params.length ? "?" + params.join("&") : ""));
    },
    wikiGenerate: function (repoId, force) {
      var body = {};
      if (repoId) body.repo = repoId;
      if (force) body.force = true;
      return postJSON("/api/wiki/generate", body);
    }
  };

  // ---- context spine ----------------------------------------------------
  var ctx = { domain: null, repoId: null, nodeId: null, symbolExtras: null, symbolTicket: null };

  // ---- confidence + provenance components -------------------------------
  function confLabel(c) { return (CONF[c] || [c])[0]; }
  function confChip(c) {
    var cls = "cl-conf cl-conf--" + String(c || "EXTRACTED").toLowerCase();
    return h("span", { class: cls },
      h("span", { class: "cl-conf__glyph", "aria-hidden": "true" }), confLabel(c));
  }
  function citeButton(receipt) {
    return h("button", {
      type: "button", class: "cl-cite", "aria-label": "Show provenance", title: "Provenance (P)",
      onclick: function () { openDrawer(receipt); }
    }, h("span", { html: icon("ui-search", "cl-ic") }));
  }
  var lastReceipt = null, drawerInvoker = null;
  function openDrawer(receipt) {
    drawerInvoker = document.activeElement;
    lastReceipt = receipt || lastReceipt;
    var body = clear($("#cl-drawer-body"));
    if (!lastReceipt) { body.appendChild(h("p", { class: "cl-muted" }, "No fact selected.")); }
    else {
      var r = lastReceipt;
      var dl = h("dl", { class: "cl-provrow" });
      function row(k, v) { dl.appendChild(h("dt", null, k)); dl.appendChild(h("dd", null, v || "—")); }
      if (r.claim) body.appendChild(h("p", { class: "cl-state__title" }, r.claim));
      if (r.confidence) body.appendChild(h("p", null, confChip(r.confidence)));
      row("Repo", r.repo);
      row("Source", r.source);
      row("Verified", r.verified_at);
      row("Extractor", r.extractor || "contextlake");
      body.appendChild(dl);
      if (r.note) body.appendChild(h("p", { class: "cl-muted" }, r.note));
      var rHref = MODE === "live" ? safeHref(r.url) : null;
      var act = rHref
        ? h("a", { class: "cl-btn", href: rHref, rel: "noopener", target: "_blank" },
          h("span", { html: icon("ui-external") }), "Jump to source")
        : h("button", {
          class: "cl-btn", type: "button",
          onclick: function () { try { navigator.clipboard.writeText(r.source || r.url || ""); live("Copied path"); } catch (e) { /* */ } }
        }, h("span", { html: icon("ui-copy") }), "Copy path");
      body.appendChild(act);
    }
    var d = $("#cl-drawer"); d.hidden = false;
    $("#cl-drawer-close").focus();
  }
  function closeDrawer() {
    $("#cl-drawer").hidden = true;
    if (drawerInvoker && drawerInvoker.focus) { try { drawerInvoker.focus(); } catch (e) { } }
    drawerInvoker = null;
  }

  // ---- state blocks -----------------------------------------------------
  var OTTER = '<span class="cl-state__pebble" role="img" aria-label="Pebble, the contextlake otter"></span>';
  // A live region only announces content that arrives AFTER it is in the document.
  // These blocks were built complete -- role="status" and its text created in the
  // same breath -- and only then appended into a panel body that is not itself a
  // live region, so every "Couldn't load this view", "Not included in this
  // snapshot" and "Pick a repo first" replaced the panel in total silence
  // (WCAG 4.1.3). The persistent #cl-live region is the one that actually speaks,
  // so route the state through it, on the next tick so the block is in the DOM
  // first. Marking the five panel bodies as live regions instead would announce
  // every full re-render, which is worse than saying nothing.
  function announceState(text) {
    if (!text) return;
    setTimeout(function () { live(text); }, 0);
  }
  function stateBlock(opts) {
    var mod = opts.kind ? " cl-state--" + opts.kind : "";
    var box = h("div", { class: "cl-state" + mod, role: "status" });
    announceState([opts.title, opts.msg].filter(Boolean).join(". "));
    if (opts.kind === "empty" || opts.kind === "ok") box.appendChild(h("span", { html: OTTER }));
    box.appendChild(h("p", { class: "cl-state__title" }, opts.title || ""));
    if (opts.msg) box.appendChild(h("p", null, opts.msg));
    if (opts.cmd) box.appendChild(h("code", null, opts.cmd));
    if (opts.action) box.appendChild(opts.action);
    return box;
  }
  // A primary action for empty states. The dashboard is read-only, so "generate"
  // means: copy the exact command to run in the terminal (then refresh). Honest by
  // design — we only offer it where contextlake can actually produce the thing.
  function genAction(label, cmd) {
    var b = h("button", { class: "cl-btn cl-btn--primary", type: "button", title: "Copy: " + cmd },
      h("span", { html: icon("ui-copy") }), label);
    b.onclick = function () {
      try { navigator.clipboard.writeText(cmd); } catch (e) { /* clipboard blocked under file:// */ }
      live("Copied “" + cmd + "”, run it in your terminal, then refresh");
      if (b.lastChild) b.lastChild.nodeValue = " Copied, run in terminal";
    };
    return b;
  }
  function skeleton(n) {
    var w = h("div", { class: "cl-panel__body", "aria-busy": "true" });
    for (var i = 0; i < (n || 3); i++) w.appendChild(h("div", { class: "cl-skeleton" }));
    return w;
  }
  function renderInto(id, node) { var b = clear($("#" + id)); b.appendChild(node); }
  function asyncPanel(bodyId, loader, render) {
    renderInto(bodyId, skeleton());
    loader().then(function (data) {
      try { renderInto(bodyId, render(data)); }
      catch (e) { renderInto(bodyId, stateBlock({ kind: "error", title: "Could not render", msg: String(e) })); }
    }).catch(function (e) {
      var staticMiss = MODE !== "live";
      renderInto(bodyId, stateBlock({
        kind: staticMiss ? "unavailable" : "error",
        title: staticMiss ? "Not included in this snapshot" : "Couldn't load this view",
        msg: staticMiss
          ? "This static export carries detail for a representative slice of repos. Run the live server to browse every repo with no caps."
          : "The data source didn't respond (" + e.message + ").",
        cmd: staticMiss ? "contextlake kb dashboard --serve" : null,
        action: staticMiss
          ? genAction("Run live server", "contextlake kb dashboard --serve")
          : h("button", { class: "cl-btn", type: "button", onclick: function () { CL.router.render(); } }, "Retry")
      }));
    });
  }

  // ---- ground-truth (confidence) filter ---------------------------------
  var gt = { EXTRACTED: true, INFERRED: true, AMBIGUOUS: true };
  function gtActive(c) { return gt[c] !== false; }

  // ---- trust bar --------------------------------------------------------
  function trustBar(byConf, opts) {
    opts = opts || {};
    var order = ["EXTRACTED", "INFERRED", "AMBIGUOUS"];
    var total = order.reduce(function (a, c) { return a + (byConf[c] || 0); }, 0) || 1;
    var track = h("div", { class: "cl-trustbar__track" });
    var keys = h("div", { class: "cl-trustbar__keys" });
    order.forEach(function (c) {
      var n = byConf[c] || 0, pct = Math.round((n / total) * 100);
      track.appendChild(h("button", {
        type: "button", class: "cl-trustbar__seg cl-trustbar__seg--" + c.toLowerCase(),
        style: "flex:" + (n || 0.001), "aria-pressed": String(gtActive(c)),
        "aria-label": confLabel(c) + " " + n + " (" + pct + "%)",
        title: confLabel(c) + " " + n + " (" + pct + "%)",
        onclick: function () { gt[c] = !gtActive(c); syncGT(); CL.router.render(); }
      }));
      keys.appendChild(h("span", null, confChip(c), " ", h("strong", null, num(n)), " · " + pct + "%"));
    });
    return h("div", { class: "cl-trustbar" }, opts.label ? h("strong", null, opts.label) : null, track, keys);
  }
  function syncGT() {
    document.querySelectorAll(".cl-gt").forEach(function (b) {
      b.setAttribute("aria-pressed", String(gtActive(b.dataset.conf)));
    });
  }

  // ---- lettermarks ------------------------------------------------------
  function lettermarks(langs) {
    var out = [];
    Object.keys(langs || {}).slice(0, 3).forEach(function (l) {
      out.push(h("span", { class: "cl-lettermark", title: l }, LANG_LABELS[l] || l.slice(0, 2).toUpperCase()));
    });
    return out;
  }

  // ===================================================================== //
  // VIEWS                                                                  //
  // ===================================================================== //

  // ---- Fleet ------------------------------------------------------------
  function viewFleet() {
    asyncPanel("fleet-body", CL.data.overview, function (ov) {
      var body = h("div", { class: "cl-panel__body" });
      var s = ov.stats || {};
      var stats = h("div", { class: "cl-statgrid" });
      [["repos", "Repos"], ["nodes", "Nodes"], ["edges", "Edges"]].forEach(function (p) {
        stats.appendChild(h("div", { class: "cl-stat" },
          h("div", { class: "cl-stat__num" }, num(s[p[0]])),
          h("div", { class: "cl-stat__cap" }, p[1])));
      });
      body.appendChild(stats);
      body.appendChild(h("div", { class: "cl-card" }, trustBar(s.by_confidence || {}, { label: "Knowledge confidence" })));

      if (!ov.repos || !ov.repos.length) {
        body.appendChild(stateBlock({
          kind: "empty", title: "No repos indexed yet",
          msg: "Index a workspace to fill the lake.", cmd: "contextlake kb index"
        }));
        return body;
      }

      var byGroup = {};
      ov.repos.forEach(function (r) { (byGroup[r.group] = byGroup[r.group] || []).push(r); });
      var groups = Object.keys(byGroup).sort();
      var openState = JSON.parse(lsGet("bands", "{}") || "{}");

      // Fleet layout: cards (rich) / list (dense rows) / table (full names, sortable look).
      var MODES = ["cards", "list", "table"];
      var viewMode = MODES.indexOf(lsGet("fleetview", "cards")) >= 0 ? lsGet("fleetview", "cards") : "cards";
      var bandsWrap = h("div");
      function repoCollection(repos) {
        if (viewMode === "table") return repoTable(repos);
        // A real <ul>/<li>, not role="list" over role="listitem" buttons -- see
        // the .cl-grid comment in dashboard.css. The card keeps its button role.
        var box = h("ul", { class: viewMode === "list" ? "cl-repolist" : "cl-grid" });
        repos.forEach(function (r) {
          box.appendChild(h("li", null, viewMode === "list" ? repoRow(r) : repoCard(r)));
        });
        return box;
      }
      function renderBands() {
        bandsWrap.textContent = "";
        groups.forEach(function (g, gi) {
          var repos = byGroup[g];
          var isOpen = openState[g] != null ? openState[g] : (gi === 0);
          var det = h("details", { class: "cl-band" });
          if (isOpen) det.open = true;
          det.appendChild(h("summary", null,
            h("span", { class: "cl-band__name" }, g),
            h("span", { class: "cl-band__count" }, repos.length + " repos")));
          // Cluster narrative (a --namespace wiki page for this group), when present
          // in the static snapshot. The html is server-sanitized by _md_to_html.
          var cl = ((window.__CONTEXTLAKE__ || {}).clusters || {})[g];
          if (cl && cl.found && cl.html) {
            det.appendChild(h("div", { class: "cl-sectionhead" },
              h("strong", null, "Cluster narrative (advisory)"),
              h("span", { class: "cl-band__count" },
                cl.member_count + " repos, " + cl.internal + " internal / "
                + cl.boundary + " boundary links")));
            det.appendChild(h("div", { class: "cl-md", html: cl.html }));
          }
          det.appendChild(repoCollection(repos));
          det.addEventListener("toggle", function () {
            openState[g] = det.open; lsSet("bands", JSON.stringify(openState));
          });
          bandsWrap.appendChild(det);
        });
      }
      var seg = h("div", { class: "cl-modeseg", role: "group", "aria-label": "Fleet layout" });
      [["cards", "Cards", "ui-cards"], ["list", "List", "ui-list"], ["table", "Table", "ui-table"]].forEach(function (m) {
        seg.appendChild(h("button", {
          type: "button", "aria-pressed": String(viewMode === m[0]), title: m[1] + " layout",
          onclick: function () {
            viewMode = m[0]; lsSet("fleetview", viewMode);
            seg.querySelectorAll("button").forEach(function (b, i) {
              b.setAttribute("aria-pressed", String(MODES[i] === viewMode));
            });
            renderBands();
          }
        }, h("span", { html: icon(m[2]) }), m[1]));
      });
      var tools = h("div", { class: "cl-row cl-fleettools" },
        h("span", { class: "cl-fleettools__label" }, "Layout"), seg);
      if (MUTATIONS) tools.appendChild(addRepoButton());
      body.appendChild(tools);
      body.appendChild(bandsWrap);
      renderBands();
      return body;
    });
  }
  function addRepoButton() {
    var btn = h("button", { class: "cl-btn", type: "button" },
      h("span", { html: icon("ui-add") }), "Add repo");
    btn.addEventListener("click", function () {
      var url = window.prompt("Git URL to clone (HTTPS, SSH, or user@host:path):");
      if (!url) return;
      if (!window.confirm("Clone " + url + " and index it?")) return;
      btn.disabled = true; btn.textContent = "Cloning…";
      CL.data.addRepo(url).then(function (r) {
        live("Added " + r.repo); go("#/repo/" + r.repo);
      }).catch(function (e) { window.alert("Add repo failed: " + e.message); })
        .then(function () { btn.disabled = false; btn.textContent = "Add repo"; });
    });
    return btn;
  }
  // Split a namespaced repo id into its basename (last segment) and parent path so the
  // name the user reads is never eaten by the long namespace prefix.
  function splitRepo(id) {
    var i = String(id).lastIndexOf("/");
    return i < 0 ? { base: id, parent: "" } : { base: id.slice(i + 1), parent: id.slice(0, i) };
  }
  function repoMeta(r) {
    return (r.node_count || 0) + " nodes · " + (r.default_branch || "—") +
      (r.head_commit ? " · " + String(r.head_commit).slice(0, 8) : "");
  }
  // Visible text for the health chip -- the chip used to be colour (well, a
  // single ::before dot) only, with a hover-only `title` tooltip as its sole
  // text channel (not reachable by touch or keyboard, not reliably exposed by
  // every screen reader). Mirrors the pattern the confidence chips already use
  // (glyph + visible label, never colour alone) -- WCAG 1.4.1.
  function healthLabel(health) { return health === "stale" ? "Stale" : "Fresh"; }
  function repoCard(r) {
    var health = r.indexed_at ? "fresh" : "stale";
    var nm = splitRepo(r.id);
    return h("button", {
      type: "button", class: "cl-repocard",
      title: r.id, onclick: function () { go("#/repo/" + r.id); }
    },
      h("div", { class: "cl-repocard__top" },
        kindIcon("repo"),
        h("span", { class: "cl-repocard__name" }, nm.base),
        h("span", { class: "cl-healthchip cl-healthchip--" + health,
          title: "Index freshness: " + health }, healthLabel(health))),
      nm.parent ? h("div", { class: "cl-repocard__path", title: r.id }, nm.parent) : null,
      h("div", { class: "cl-repocard__meta" },
        lettermarks(r.langs),
        h("span", { class: "cl-repocard__stat", title: "Graph nodes in this repo" },
          (r.node_count || 0) + " nodes"),
        h("span", { class: "cl-repocard__stat" }, r.default_branch || "—")));
  }
  function repoRow(r) {
    var health = r.indexed_at ? "fresh" : "stale";
    var nm = splitRepo(r.id);
    return h("button", {
      type: "button", class: "cl-reporow",
      title: r.id, onclick: function () { go("#/repo/" + r.id); }
    },
      kindIcon("repo"),
      h("span", { class: "cl-reporow__name" }, nm.base),
      h("span", { class: "cl-reporow__path" }, nm.parent),
      h("span", { class: "cl-reporow__meta" }, (r.node_count || 0) + " nodes"),
      h("span", { class: "cl-reporow__meta" }, r.default_branch || "—"),
      lettermarks(r.langs),
      h("span", { class: "cl-healthchip cl-healthchip--" + health, title: health }, healthLabel(health)));
  }
  function repoTable(repos) {
    var tb = h("tbody");
    repos.forEach(function (r) {
      var health = r.indexed_at ? "fresh" : "stale";
      var nav = function () { go("#/repo/" + r.id); };
      tb.appendChild(h("tr", {
        tabindex: "0", title: r.id, onclick: nav,
        onkeydown: function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); nav(); } }
      },
        h("td", { class: "cl-repotable__name" }, r.id),
        h("td", null, lettermarks(r.langs)),
        h("td", { class: "cl-num" }, String(r.node_count || 0)),
        h("td", null, r.default_branch || "—"),
        h("td", null, h("span", { class: "cl-healthchip cl-healthchip--" + health, title: health }, healthLabel(health)))));
    });
    return h("table", { class: "cl-repotable" },
      h("thead", null, h("tr", null,
        h("th", null, "Repository"), h("th", null, "Lang"),
        h("th", { class: "cl-num" }, "Nodes"), h("th", null, "Branch"), h("th", null, "Health"))),
      tb);
  }

  // ---- Repo detail ------------------------------------------------------
  function syncRepoButton(id) {
    var btn = h("button", { class: "cl-btn", type: "button" }, h("span", { html: icon("ui-refresh") }), "Sync now");
    btn.addEventListener("click", function () {
      if (!window.confirm("git pull and reindex " + id + "?")) return;
      btn.disabled = true; btn.textContent = "Syncing…";
      CL.data.syncRepo(id).then(function (r) {
        live(r.changed === false ? "Already up to date" : "Synced " + id);
        go("#/repo/" + id);
      }).catch(function (e) { window.alert("Sync failed: " + e.message); })
        .then(function () { btn.disabled = false; btn.textContent = "Sync now"; });
    });
    return btn;
  }
  function viewRepo(id, tab) {
    if (!id) {
      renderInto("repo-body", stateBlock({
        kind: "empty", title: "Pick a repo first",
        msg: "Choose a repo from the fleet to see its anatomy.",
        action: h("button", { class: "cl-btn cl-btn--primary", type: "button", onclick: function () { go("#/fleet"); } }, "Open fleet")
      }));
      return;
    }
    asyncPanel("repo-body", function () { return CL.data.repo(id); }, function (d) {
      ctx.repoId = id; refreshChrome();
      var body = h("div", { class: "cl-panel__body" });
      var b = d.brief || {};
      body.appendChild(h("div", { class: "cl-sectionhead" },
        h("div", { class: "cl-row" }, kindIcon("repo"), h("strong", null, d.repo),
          lettermarks(b.langs), h("span", { class: "cl-healthchip cl-healthchip--fresh" }, b.head ? "@ " + String(b.head).slice(0, 8) : "indexed")),
        h("div", { class: "cl-row" },
          h("button", { class: "cl-btn", type: "button", onclick: function () { ctx.repoId = id; refreshChrome(); live("Pinned " + id); } },
            h("span", { html: icon("ui-pin") }), "Pin"),
          MUTATIONS ? syncRepoButton(id) : null,
          h("button", { class: "cl-btn cl-btn--primary", type: "button", onclick: function () { go("#/arch/" + id); } },
            h("span", { html: icon("ui-arch") }), "View in architecture"))));

      // "docs" is live-only: unlike the wiki, no generated document is bundled into the
      // repo-detail payload, so a static export has nothing to put in the tab. Offering
      // one that can never fill reads as broken in a published snapshot, so it is left
      // out there rather than shown empty. renderDocsTab still handles the static case,
      // for a deep link that names ?tab=docs directly.
      var tabs = ["anatomy", "readme", "wiki", "docs", "owners", "links", "diagrams"]
        .filter(function (t) { return t !== "docs" || MODE !== "static"; });
      var cur = tabs.indexOf(tab) >= 0 ? tab : "anatomy";
      // role="group" + aria-pressed, not tablist/tab: there is no tabpanel in this
      // document and no roving tabindex, so the tab roles promised a structure that
      // did not exist (WCAG 4.1.2 / 1.3.1). These are toggle buttons.
      var strip = h("div", { class: "cl-tabs", role: "group", "aria-label": "Repo sections" });
      var pane = h("div", { class: "cl-panel__body" });
      tabs.forEach(function (t) {
        strip.appendChild(h("button", {
          class: "cl-tab", type: "button", "aria-pressed": String(t === cur),
          onclick: function () { go("#/repo/" + id + "?tab=" + t); }
        }, t[0].toUpperCase() + t.slice(1)));
      });
      body.appendChild(strip); body.appendChild(pane);
      renderRepoTab(pane, cur, d, id);
      return body;
    });
  }
  function renderRepoTab(pane, tab, d, id) {
    clear(pane);
    var b = d.brief || {};
    if (tab === "anatomy") {
      if (!b || !b.node_count) { pane.appendChild(stateBlock({ kind: "empty", title: "No anatomy", msg: "This repo has no parsed symbols." })); return; }
      var kinds = b.kinds || {};
      var klist = h("div", { class: "cl-card" }, h("strong", null, "Kinds"));
      Object.keys(kinds).sort(function (a, c) { return kinds[c] - kinds[a]; }).forEach(function (k) {
        klist.appendChild(h("div", { class: "cl-row" }, kindIcon(k), k, h("span", { class: "cl-muted" }, String(kinds[k]))));
      });
      pane.appendChild(klist);
      var top = h("div", { class: "cl-card" }, h("strong", null, "Top symbols"));
      (b.top_symbols || []).forEach(function (t) {
        var row = h("div", { class: "cl-row" }, kindIcon(t.kind), h("strong", null, t.name),
          t.file ? h("span", { class: "cl-muted" }, t.file) : null,
          h("button", { class: "cl-btn", type: "button", onclick: function () { go("#/symbol/" + (t.name || "")); } },
            h("span", { html: icon("ui-blast") }), "Blast radius"),
          citeButton({ claim: t.name, repo: d.repo, source: t.file || "—", confidence: "EXTRACTED", note: t.signature || "" }));
        top.appendChild(row);
      });
      if (b.top_symbols && b.top_symbols.length) pane.appendChild(top);
      // Hotspots: fan-in (hubs) and fan-out (dispatchers) ranked separately --
      // the combined-degree "Top symbols" above answers "what matters", this
      // answers "where's the risk" (hubs: protect with tests; dispatchers:
      // where behavior branches). Data already computed at index time
      // (wiki.generate.repo_brief); this is its own section, not folded in.
      var hubs = b.hubs || [], dispatchers = b.dispatchers || [];
      if (hubs.length || dispatchers.length) {
        var hotWrap = h("div", { class: "cl-card" }, h("strong", null, "Hotspots"));
        function hotTable(title, rows, countLabel) {
          if (!rows.length) return null;
          return h("div", null, h("p", { class: "cl-muted" }, title),
            table(["symbol", "file", countLabel, ""], rows.map(function (t) {
              return [t.name, t.file || "—", String(t.count),
                h("button", { class: "cl-btn", type: "button", onclick: function () { go("#/symbol/" + (t.name || "")); } },
                  h("span", { html: icon("ui-blast") }), "Blast radius")];
            }), [false, false, true, false]));
        }
        var hubsTable = hotTable("Most depended on (hubs)", hubs, "callers");
        var dispatchersTable = hotTable("Widest fan-out (dispatchers)", dispatchers, "callees");
        if (hubsTable) hotWrap.appendChild(hubsTable);
        if (dispatchersTable) hotWrap.appendChild(dispatchersTable);
        pane.appendChild(hotWrap);
      }
    } else if (tab === "readme") {
      if (d.readme_html) pane.appendChild(h("div", { class: "cl-card" },
        h("p", { class: "cl-muted" }, "README — verbatim from source" + (b.head ? " @ " + String(b.head).slice(0, 8) : "")),
        h("div", { class: "cl-md", html: d.readme_html })));
      else pane.appendChild(stateBlock({ kind: "empty", title: "No README found in this repo" }));
    } else if (tab === "wiki") {
      renderWikiTab(pane, d, id);
    } else if (tab === "docs") {
      renderDocsTab(pane, d, id);
    } else if (tab === "owners") {
      if (!d.owners || !d.owners.length) { pane.appendChild(stateBlock({ kind: "empty", title: "No owners", msg: "Derived from git history — none available." })); return; }
      var ot = table(["Owner", "Commits", "Lines", "Share", ""],
        d.owners.map(function (o) {
          return [o.name, num(o.commits), num(o.lines), Math.round((o.share || 0) * 100) + "%",
            citeButton({ claim: o.name + " owns code here", repo: d.repo, source: "git history", verified_at: o.last_active, extractor: "git-blame", confidence: "INFERRED" })];
        }), [false, true, true, true, false]);
      pane.appendChild(h("p", { class: "cl-muted" }, "Ranked from git history."));
      pane.appendChild(ot);
    } else if (tab === "links") {
      var groups = d.links || {};
      var keys = Object.keys(groups);
      if (!keys.length) { pane.appendChild(stateBlock({ kind: "empty", title: "No connector links found", msg: "No Jira / Confluence / Figma / GitLab / Slack cross-links." })); return; }
      keys.forEach(function (rel) {
        var card = h("div", { class: "cl-card" }, h("strong", null, rel.replace(/_/g, " ")));
        groups[rel].forEach(function (l) {
          var lHref = MODE === "live" ? safeHref(l.url) : null;
          var act = lHref
            ? h("a", { class: "cl-btn", href: lHref, rel: "noopener", target: "_blank" }, h("span", { html: icon("ui-external") }), "Open")
            : h("button", { class: "cl-btn", type: "button", onclick: function () { try { navigator.clipboard.writeText(l.url || ""); live("Copied"); } catch (e) { } } }, h("span", { html: icon("ui-copy") }), "Copy path");
          card.appendChild(h("div", { class: "cl-row" }, kindIcon(l.kind), h("strong", null, l.title || l.name),
            l.status ? h("span", { class: "cl-muted" }, l.status) : null, confChip(l.confidence), l.url ? act : null));
        });
        pane.appendChild(card);
      });
    } else if (tab === "diagrams") {
      renderDiagramsTab(pane, d, id);
    }
  }

  // ---- Wiki (repo page) --------------------------------------------------
  // The repo-overview page arrives bundled in `d.wiki` (repo_detail's own
  // `_wiki_out` call) -- no extra fetch needed for it. A federated repo's
  // per-subsystem pages (Task 15/16) are a flat, top-level pick (repo_modules()
  // with no `within` -- unlike the Diagrams tab's recursive drill-down, a wiki
  // page is never "too large to render", it's just a choice between distinct
  // advisory pages, so there's no breadcrumb/auto-descend machinery here).
  // Picking a module re-fetches via CL.data.repoWiki(id, module) -- its own
  // lightweight route -- and swaps just this section in place; picking back to
  // "whole repo" reuses the already-fetched `d.wiki` rather than refetching it.
  function wikiEmptyState(id, forWhat) {
    return stateBlock({
      kind: "empty", title: "No wiki generated for " + forWhat + " yet",
      msg: "Generate a curated wiki from this repo's code and history, then refresh.",
      cmd: "contextlake kb wiki " + id + " --llm builtin",
      action: genAction("Generate wiki", "contextlake kb wiki " + id + " --llm builtin")
    });
  }
  function wikiContentNode(w, id, forWhat) {
    if (!w || !w.found) return wikiEmptyState(id, forWhat);
    var wikiHeader = [h("strong", null, "Curated wiki — advisory")];
    if (w.stale) wikiHeader.push(h("span", { class: "cl-healthchip cl-healthchip--stale" },
      "STALE — code changed since generation"));
    return h("div", { class: "cl-advisory" },
      h("div", { class: "cl-row" }, wikiHeader),
      h("p", { class: "cl-muted" }, "Not ground truth — verify against the cited source."),
      h("div", { class: "cl-md", html: w.html || "" }));
  }
  function renderWikiTab(pane, d, id) {
    var contentWrap = h("div", null, wikiContentNode(d.wiki, id, "this repo"));
    pane.appendChild(contentWrap);
    if (MUTATIONS) pane.appendChild(wikiRegenerateCard(id));

    if (MODE === "static") return; // no /modules route to ask -- see CL.data.repoModules
    // repoModules(id, null, true) asks the server for a `has_page` flag per
    // module -- whether wiki generation actually wrote THAT module a subsystem
    // page on disk. Filtering to has_page modules (rather than the raw list,
    // which is just "top-level dirs with enough nodes", the same one the
    // Diagrams tab uses) means the picker only ever offers options that will
    // actually resolve: a small non-federated repo's ordinary src/tests dirs
    // are never wiki-federated so filter to [] and the picker doesn't render;
    // a large federated repo whose module count exceeds the generator's
    // _MAX_MODULE_PAGES_PER_REPO cap only offers the subset that got a page,
    // never the stranded tail that would 404 forever. This also drops the
    // previous "probe the largest module" heuristic entirely -- that approach
    // would have suppressed the WHOLE picker if the largest module's page
    // specifically got rejected by the council while smaller ones succeeded;
    // per-module has_page has no such blind spot.
    CL.data.repoModules(id, null, true).then(function (res) {
      var mods = ((res && res.modules) || []).filter(function (m) { return m.has_page; });
      if (!mods.length) return; // no generated subsystem pages -- degrade to nothing (no picker)
      renderPicker(mods);
    }).catch(function () { /* module listing is a convenience, not essential -- fail quiet */ });

    function renderPicker(mods) {
      var loadSeq = 0;
      var select = h("select", {
        class: "cl-select", "aria-label": "Wiki scope",
        onchange: function (ev) {
          var mod = ev.target.value || null;
          var mySeq = ++loadSeq;
          clear(contentWrap);
          contentWrap.appendChild(skeleton(1));
          var fetchWiki = mod ? CL.data.repoWiki(id, mod) : Promise.resolve(d.wiki);
          fetchWiki.then(function (w) {
            if (mySeq !== loadSeq) return; // a newer selection superseded this one
            clear(contentWrap);
            contentWrap.appendChild(wikiContentNode(w, id, mod ? "module “" + mod + "”" : "this repo"));
          }).catch(function (e) {
            if (mySeq !== loadSeq) return;
            clear(contentWrap);
            contentWrap.appendChild(stateBlock({
              kind: "error", title: "Couldn't load this wiki page", msg: String(e)
            }));
          });
        }
      }, [h("option", { value: "" }, "Whole repo — overview")].concat(
        mods.map(function (m) {
          return h("option", { value: m.prefix }, m.prefix + " (" + m.nodes + ")");
        })
      ));
      pane.insertBefore(h("div", { class: "cl-row" },
        h("label", { class: "cl-muted" }, "Subsystem:"), select), contentWrap);
    }
  }

  // ---- Documents (repo page) ---------------------------------------------
  // `kb docs` writes two pages per repo and neither involves a model: every line
  // traces to an edge a parser recorded. That is the whole reason this tab reads
  // differently from Wiki next door -- the wiki carries an advisory caveat because
  // it is synthesized prose, and repeating that caveat here would understate these.
  // What they DO share is staleness, so the same chip appears for the same reason.
  var DOC_KINDS = [
    { key: "api", label: "API reference", blurb: "Every callable symbol, with the file-and-line call sites the graph recorded." },
    { key: "design", label: "Design notes", blurb: "What the repo's own files record: declared dependencies, and the values its code reads most." }
  ];
  function docsEmptyState(id, kind) {
    return stateBlock({
      kind: "empty",
      title: "No " + kind.label.toLowerCase() + " generated for this repo yet",
      msg: "Built from the graph, with no model involved.",
      cmd: "contextlake kb docs",
      action: genAction("Generate documents", "contextlake kb docs")
    });
  }
  function docsContentNode(doc, id, kind) {
    if (!doc || !doc.found) return docsEmptyState(id, kind);
    var head = [h("strong", null, kind.label)];
    // Three states, not two. `stamp.py` distinguishes an absent marker ("nothing to
    // report") from a present commit=unknown ("checked, could not determine"), and
    // collapsing them would throw away the difference it went to trouble to keep.
    if (doc.stale) {
      var why;
      if (!doc.doc_commit) why = "STALE — this page carries no commit";
      else if (doc.doc_commit === "unknown") why = "STALE — generated at an unknown commit";
      else why = "STALE — generated at " + String(doc.doc_commit).slice(0, 8);
      head.push(h("span", { class: "cl-healthchip cl-healthchip--stale" }, why));
    }
    return h("div", { class: "cl-card" },
      h("div", { class: "cl-row" }, head),
      h("p", { class: "cl-muted" }, kind.blurb + " No model was involved."),
      h("div", { class: "cl-md", html: doc.html || "" }));
  }
  function renderDocsTab(pane, d, id) {
    if (MODE === "static") {
      pane.appendChild(stateBlock({
        kind: "empty", title: "Generated documents are live-only",
        msg: "The static export does not carry them. Run the dashboard against the store to read them."
      }));
      return;
    }
    var current = "api";
    var strip = h("div", { class: "cl-tabs", role: "group", "aria-label": "Document kind" });
    var body = h("div", null, stateBlock({ kind: "loading", title: "Loading…" }));

    function load(key) {
      current = key;
      Array.prototype.forEach.call(strip.children, function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.kind === key));
      });
      body.replaceChildren(stateBlock({ kind: "loading", title: "Loading…" }));
      var kind = DOC_KINDS.filter(function (k) { return k.key === key; })[0];
      CL.data.repoDocs(id, key).then(function (doc) {
        if (current !== key) return;   // a later click already won
        body.replaceChildren(docsContentNode(doc, id, kind));
      }).catch(function (e) {
        if (current !== key) return;
        body.replaceChildren(stateBlock({
          kind: "error", title: "Could not load the " + kind.label.toLowerCase(),
          msg: String((e && e.message) || e)
        }));
      });
    }

    DOC_KINDS.forEach(function (k) {
      var b = h("button", {
        class: "cl-tab", type: "button", "aria-pressed": String(k.key === current),
        onclick: function () { load(k.key); }
      }, k.label);
      b.dataset.kind = k.key;
      strip.appendChild(b);
    });
    pane.appendChild(strip);
    pane.appendChild(body);
    load("api");
  }

  // ---- Diagrams (repo page) ----------------------------------------------
  // Formats offered here mirror `contextlake graph --repo <id> --format <fmt>` --
  // same payload, same renderers (kb/visualize/), nothing new extracted. Each
  // renderer already degrades to an honest "no X in this view" placeholder, but
  // `avail` still gates the tab itself so the switcher only offers formats that
  // are actually meaningful for this repo (using kinds the anatomy tab already
  // fetched). sequencediagram is deliberately absent: it needs a single symbol
  // seed, not a repo-wide view (see kb/dashboard/data.py's DIAGRAM_FORMATS).
  var DIAGRAM_FORMATS = [
    { fmt: "mermaid", label: "Relations", avail: function () { return true; } },
    { fmt: "classdiagram", label: "Classes",
      avail: function (k) { return !!(k.class || k.interface || k.struct || k.enum); } },
    { fmt: "statediagram", label: "States", avail: function (k) { return !!k.state; } },
    { fmt: "erdiagram", label: "Data model", avail: function (k) { return !!(k.table || k.view); } },
    { fmt: "deploymentdiagram", label: "Deployment",
      avail: function (k) { return !!(k.resource || k.data); } }
  ];
  var mermaidLoadPromise = null;
  function loadMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (mermaidLoadPromise) return mermaidLoadPromise;
    mermaidLoadPromise = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = "mermaid.min.js";
      s.onload = function () {
        // strict: no click/tooltip callbacks execute from rendered diagram text --
        // repo content (symbol names, table names, ...) is untrusted input. Theme
        // is (re-)applied per render in mermaidCard, not here, so a light/dark
        // toggle takes effect on the next render without reloading the script.
        // maxEdges is a belt-and-braces margin above repo_subgraph()'s own
        // max_edges=400 server-side cap (see kb/visualize/payload.py) -- the
        // server should never actually hand back more than ~400-500 edges, but
        // a future caller (CLI-generated text pasted in, an older cached
        // response) shouldn't hard-error instead of just rendering slower.
        window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", maxEdges: 2000 });
        resolve(window.mermaid);
      };
      s.onerror = function () { reject(new Error("failed to load mermaid.min.js")); };
      document.head.appendChild(s);
    });
    return mermaidLoadPromise;
  }
  var mermaidRenderSeq = 0;
  // On a failed render, mermaid.js leaves its temporary "#d<id>{...}" wrapper
  // (a <div> holding the diagram's injected <style>) sitting directly in
  // document.body -- it only cleans this up itself on success. Left alone,
  // every failed render (e.g. hitting Mermaid's own maxEdges guard on a huge
  // repo) permanently adds one more global stylesheet the whole page's CSS
  // engine must consider on every recalc, so a session with several failed
  // attempts gets measurably, cumulatively slower -- not just that one
  // diagram staying broken. Removed defensively regardless of failure cause.
  function cleanupMermaidLeak(renderId) {
    var leaked = document.getElementById("d" + renderId);
    if (leaked) leaked.remove();
  }
  function mermaidCard(text) {
    var svgBox = h("div", { class: "cl-mermaid" }, h("p", { class: "cl-muted" }, "Rendering…"));
    var seq = ++mermaidRenderSeq;
    var renderId = "cl-mmd-" + seq;
    loadMermaid()
      .then(function (mermaid) {
        // re-applied on every render (not just at load) so a diagram opened
        // before a light/dark toggle still switches theme on the next render.
        // maxEdges is repeated here too -- mermaid.initialize() replaces the
        // whole config each call, it doesn't merge, so omitting it here would
        // silently fall back to the default 500 on every re-theme.
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", maxEdges: 2000,
          theme: document.documentElement.dataset.theme === "dark" ? "dark" : "default" });
        return mermaid.render(renderId, text);
      })
      .then(function (out) {
        if (seq !== mermaidRenderSeq) return; // a newer render superseded this one
        // out.svg is already DOMPurify-sanitized by mermaid itself (securityLevel
        // "strict" above) -- this is mermaid's own documented safe integration
        // point for untrusted diagram text (repo symbol/table/resource names).
        clear(svgBox); svgBox.innerHTML = out.svg;
      })
      .catch(function (e) {
        cleanupMermaidLeak(renderId);
        if (seq !== mermaidRenderSeq) return;
        clear(svgBox);
        svgBox.appendChild(stateBlock({
          kind: "error", title: "Couldn't render this diagram",
          msg: String((e && e.message) || e)
        }));
      });
    return h("div", { class: "cl-panel__body" }, svgBox, copyCard("Mermaid source", text));
  }
  function renderDiagramsTab(pane, d, id) {
    if (MODE === "static") { pane.appendChild(stateBlock({
      kind: "unavailable", title: "Diagrams are live-only",
      msg: "This static export has no running server behind it.",
      cmd: "contextlake kb dashboard --serve",
      action: genAction("Run live server", "contextlake kb dashboard --serve")
    })); return; }
    var kinds = (d.brief && d.brief.kinds) || {};
    var available = {};
    DIAGRAM_FORMATS.forEach(function (f) { available[f.fmt] = f.avail(kinds); });
    var strip = h("div", { class: "cl-tabs", role: "group", "aria-label": "Diagram format" });
    var scopeWrap = h("div", { class: "cl-diagram-scope" });
    var body = h("div", { class: "cl-panel__body" });
    var currentModule = null;
    var modulesCache = {}; // within-value ("" = top level) -> promise of {modules:[...]}
    // The initial landing auto-descends into the largest child at each level,
    // not just one hop, until the view is no longer truncated or there's
    // nowhere further to drill (see repo_modules()'s `within` param) -- e.g. a
    // repo whose entire code lives under one top-level dir used to dead-end
    // picking that one module (still truncated, no way further down); now it
    // keeps descending into that module's own largest child, and so on. Runs
    // once per tab load: after it settles, or the user makes any explicit
    // pick, further truncated views just offer the picker -- no repeated
    // silent auto-navigation out from under an explicit choice.
    var autoDefaultApplied = false;

    // Only fetched once a diagram actually comes back truncated -- a repo small
    // enough to render whole never pays for this extra round trip. Cached per
    // scope depth so re-visiting an already-seen level (e.g. via a breadcrumb
    // click) doesn't refetch.
    function loadModules(within) {
      var key = within || "";
      if (!modulesCache[key]) modulesCache[key] = CL.data.repoModules(id, within).catch(function () {
        return { modules: [] }; // scope-down is a convenience, not essential -- fail quiet
      });
      return modulesCache[key];
    }

    // "src/payments" -> ["src", "src/payments"], each entry a full prefix
    // ready to jump back to.
    function crumbPrefixes(mod) {
      if (!mod) return [];
      var parts = mod.split("/"), out = [], acc = "";
      parts.forEach(function (p) { acc = acc ? acc + "/" + p : p; out.push(acc); });
      return out;
    }

    function jumpTo(prefix) {
      autoDefaultApplied = true; // an explicit choice -- never auto-navigate away from it
      currentModule = prefix;
      renderFmt(currentFmt);
    }

    // Caller clears scopeWrap first -- rebuilt fresh on every render so the
    // breadcrumb/picker stay synced to currentModule (which can change via the
    // auto-drill above, not just a user click). `mods` are currentModule's own
    // children (one level deeper); `truncated` is whether the CURRENT view
    // still needs narrowing.
    function renderScopeControls(mods, truncated) {
      clear(scopeWrap);
      if (currentModule) {
        var crumbs = crumbPrefixes(currentModule);
        var trail = [h("button", {
          class: "cl-crumb", type: "button", onclick: function () { jumpTo(null); }
        }, "Whole repo")];
        crumbs.forEach(function (full, i) {
          trail.push(h("span", { class: "cl-crumb-sep" }, "›"));
          var label = full.split("/").pop();
          trail.push(i === crumbs.length - 1
            ? h("span", { class: "cl-crumb cl-crumb--current" }, label)
            : h("button", {
                class: "cl-crumb", type: "button",
                onclick: (function (p) { return function () { jumpTo(p); }; })(full)
              }, label));
        });
        scopeWrap.appendChild(h("div", { class: "cl-crumbs" }, trail));
      }
      if (!mods.length) {
        if (truncated) scopeWrap.appendChild(h("p", { class: "cl-muted" },
          "Still too large to show in full — no further breakdown available for " +
          (currentModule ? "this module" : "this repo") + "."));
        return;
      }
      var select = h("select", {
        class: "cl-select", "aria-label": "Narrow further",
        onchange: function (ev) { if (ev.target.value) jumpTo(ev.target.value); }
      }, h("option", { value: "" }, "Narrow further…"),
         mods.map(function (m) {
           return h("option", { value: m.prefix }, m.prefix.split("/").pop() + " (" + m.nodes + ")");
         }));
      scopeWrap.appendChild(h("label", { class: "cl-diagram-scope__label" },
        (currentModule ? "This module" : "This repo") + " is too large to show in one diagram — ", select));
    }

    var currentFmt = null;
    // The auto-drill can take several sequential round trips (a deeply nested
    // repo). If the user switches format tabs mid-descent, that's a brand new
    // renderFmt() call while the old chain's fetches are still in flight --
    // without a guard, the stale chain's `.then()`s would go on mutating
    // `currentModule`/the DOM after a newer chain already took over. Same
    // stale-response guard idiom as `mermaidRenderSeq` elsewhere in this file.
    var renderGen = 0;
    function renderFmt(fmt) {
      currentFmt = fmt;
      var myGen = ++renderGen;
      strip.querySelectorAll(".cl-tab").forEach(function (btn) {
        btn.setAttribute("aria-pressed", String(btn.dataset.fmt === fmt));
      });
      clear(body); body.appendChild(skeleton(1));
      CL.data.diagram(id, fmt, currentModule).then(function (res) {
        if (myGen !== renderGen) return; // a newer renderFmt call superseded this one
        if (res.error) { clear(body); body.appendChild(stateBlock({ kind: "error", title: "Unknown diagram format" })); return; }
        if (!res.truncated) {
          autoDefaultApplied = true;
          renderScopeControls([], false); // still show the breadcrumb if scoped, so the user keeps context
          clear(body); body.appendChild(mermaidCard(res.text));
          return;
        }
        loadModules(currentModule).then(function (modRes) {
          if (myGen !== renderGen) return;
          var mods = modRes.modules || [];
          if (!autoDefaultApplied && mods.length) {
            currentModule = mods[0].prefix; // largest child at this depth -- see repo_modules()'s own ranking
            renderFmt(fmt); // re-fetch scoped one level deeper instead of rendering the still-too-big slice
            return;
          }
          autoDefaultApplied = true;
          renderScopeControls(mods, true);
          clear(body); body.appendChild(mermaidCard(res.text));
        });
      }).catch(function (e) {
        if (myGen !== renderGen) return;
        clear(body);
        body.appendChild(stateBlock({ kind: "error", title: "Couldn't load this diagram", msg: String(e) }));
      });
    }
    DIAGRAM_FORMATS.forEach(function (f) {
      var isAvail = available[f.fmt];
      var btn = h("button", {
        class: "cl-tab", type: "button", "aria-pressed": "false",
        dataset: { fmt: f.fmt }, disabled: !isAvail,
        title: isAvail ? null : ("No " + f.label.toLowerCase() + " data in this repo")
      }, f.label);
      if (isAvail) btn.addEventListener("click", function () { renderFmt(f.fmt); });
      strip.appendChild(btn);
    });
    pane.appendChild(strip); pane.appendChild(scopeWrap); pane.appendChild(body);
    var firstFmt = DIAGRAM_FORMATS.filter(function (f) { return available[f.fmt]; })[0] || DIAGRAM_FORMATS[0];
    renderFmt(firstFmt.fmt);
  }

  // ---- Architecture -----------------------------------------------------
  function viewArch(id) {
    ctx.repoId = id || ctx.repoId; refreshChrome();
    var body = clear($("#arch-body"));
    var scope = id ? "repo" : "overview";
    var theme = document.documentElement.dataset.theme || "light";
    // The graph honors prefers-color-scheme at load (Tier-1 floor). Only surface the
    // seam note when the in-app theme DIVERGES from the system setting — when they agree
    // there is no visible seam to explain (spec §9 / N2).
    var sysTheme = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
    var themeDiverges = theme !== sysTheme;
    body.appendChild(h("div", { class: "cl-graphtoolbar", role: "toolbar", "aria-label": "Graph scope" },
      h("button", { class: "cl-btn", type: "button", "aria-pressed": String(scope === "overview"), onclick: function () { go("#/arch"); } }, "Overview"),
      h("button", { class: "cl-btn", type: "button", "aria-pressed": String(scope === "repo"), disabled: !id, onclick: function () { if (ctx.repoId) go("#/arch/" + ctx.repoId); } }, "This repo"),
      h("a", { class: "cl-btn", href: graphSrc(scope, id), target: "_blank", rel: "noopener" }, h("span", { html: icon("ui-external") }), "Fullscreen"),
      themeDiverges ? h("span", { class: "cl-graphseam" }, "Graph follows your system colour setting.") : null));
    body.appendChild(h("a", { class: "cl-skip", href: "#arch-tables" }, "Skip past graph"));
    var frame = h("iframe", {
      class: "cl-graphframe", id: "cl-iframe", title: "Architecture graph (" + scope + ")",
      src: graphSrc(scope, id) + "?theme=" + theme, loading: "lazy"
    });
    body.appendChild(frame);

    var tablesWrap = h("div", { id: "arch-tables" });
    body.appendChild(tablesWrap);
    var target = id || ctx.repoId;
    tablesWrap.appendChild(skeleton(2));
    // At fleet scope (no repo picked) there's no data_flow tab -- that relation
    // is intra-repo only (see CL.data.dataFlow's comment) with no fleet-wide
    // equivalent to show. dataFlow itself is a separate, live-only fetch
    // (different row shape -- file->table, not a repo-pair edge); a
    // static-mode/offline rejection degrades that ONE tab to "unavailable",
    // not the whole tables section.
    var relPromise = target ? CL.data.rel(target) : CL.data.fleetRel();
    var dataFlowPromise = target ? CL.data.dataFlow(target).catch(function () { return null; }) : Promise.resolve(null);
    Promise.all([relPromise, dataFlowPromise]).then(function (results) {
      var rel = results[0], dataFlow = results[1];
      var dataFlowRows = dataFlow ? dataFlow.rows : null;
      clear(tablesWrap);
      // Fleet-wide relationships are the text/table equivalent of the whole-fleet
      // Overview graph (WCAG 1.1.1) -- same three repo-pair categories as a single
      // repo's tables, just unfiltered by repo. No data_flow tab at this scope.
      if (!target && rel.truncated) {
        tablesWrap.appendChild(h("div", { class: "cl-truncbanner" },
          "Showing the first 500 of each relationship type -- narrow to a single repo to see the rest."));
      }
      var sub = target ? ["dependencies", "http_flow", "event_flow", "data_flow"] : ["dependencies", "http_flow", "event_flow"];
      var names = { dependencies: "Dependencies", http_flow: "HTTP flow", event_flow: "Event flow", data_flow: "Data flow" };
      var cur = "dependencies";
      var strip = h("div", { class: "cl-tabs", role: "group", "aria-label": "Relationship kind" });
      var pane = h("div", null);
      function paintDataFlow() {
        if (dataFlowRows === null) {
          pane.appendChild(stateBlock({
            kind: "unavailable", title: "Live server required",
            msg: "Run the live dashboard to see file-level table reads/writes.",
            cmd: "contextlake kb dashboard --serve"
          }));
          return;
        }
        if (!dataFlowRows.length) { pane.appendChild(stateBlock({ kind: "empty", title: "No data flow detected in this repo" })); return; }
        if (dataFlow.truncated) pane.appendChild(h("div", { class: "cl-truncbanner" }, "Showing first " + dataFlowRows.length + " -- narrow the repo or read the graph directly to see the rest."));
        pane.appendChild(table(["File", "Line", "Table/view", "Relation", ""],
          dataFlowRows.map(function (e) {
            return [e.file || "?", e.line || "", e.table, e.relation,
              citeButton({ claim: (e.file || "?") + " " + e.relation + " " + e.table, repo: target, source: "kb/flow/data.py (regex-extracted SQL)", confidence: "EXTRACTED" })];
          }), [false, true, false, false, false]));
      }
      function paint(k) {
        clear(pane);
        if (k === "data_flow") { paintDataFlow(); return; }
        var rows = (rel[k] || []).filter(function (e) { return gtActive(e.confidence); });
        if (!rows.length) { pane.appendChild(stateBlock({ kind: "empty", title: "No " + names[k].toLowerCase() + " for this scope" })); return; }
        var maxW = rows.reduce(function (a, e) { return Math.max(a, e.weight || 1); }, 1);
        pane.appendChild(table(["Source", "Target", "Relation", "Confidence", "Weight", ""],
          rows.map(function (e) {
            return [e.src, e.dst, e.relation, confChip(e.confidence),
              // The bar alone left the Weight cell empty to a screen reader and
              // uncomparable to a magnifier user (WCAG 1.1.1). The number is the
              // content; the bar is decoration beside it.
              h("span", { class: "cl-flowcell" },
                h("span", { class: "cl-flowbar", "aria-hidden": "true",
                  style: "width:" + Math.max(6, Math.round((e.weight || 1) / maxW * 60)) + "px" }),
                h("span", null, num(e.weight || 1))),
              citeButton({ claim: e.src + " → " + e.dst, repo: e.src, source: e.context || "manifest/regex", confidence: e.confidence, note: e.confidence === "AMBIGUOUS" ? "Flagged uncertain — verify." : "" })];
          }), [false, false, false, false, true, false],
          rows.map(function (e) { return e.confidence === "AMBIGUOUS"; })));
      }
      sub.forEach(function (k) {
        // data_flow with dataFlowRows === null means "unavailable" (static mode), not "zero" --
        // a bare "(0)" would misreport a repo as having no data flow when it's just unfetched.
        var label = names[k];
        if (k === "data_flow" && dataFlowRows === null) { label += " (unavailable)"; }
        else { label += " (" + (k === "data_flow" ? dataFlowRows.length : (rel[k] || []).length) + ")"; }
        strip.appendChild(h("button", {
          class: "cl-tab", type: "button", "aria-pressed": String(k === cur),
          onclick: function () { strip.querySelectorAll(".cl-tab").forEach(function (t) { t.setAttribute("aria-pressed", "false"); }); this.setAttribute("aria-pressed", "true"); paint(k); }
        }, label));
      });
      tablesWrap.appendChild(strip); tablesWrap.appendChild(pane); paint(cur);
    }).catch(function () {
      clear(tablesWrap);
      if (target) {
        tablesWrap.appendChild(stateBlock({ kind: "error", title: "Couldn't load relationships" }));
      } else {
        // Most likely an older static export built before fleet-wide relationships
        // existed (SNAP has no `fleet_relationships` key) -- that's a missing
        // feature in an old snapshot, not a load error, so this degrades to the
        // original invitation rather than an alarming error state. Picking a repo
        // still gives full parity via the per-repo tables above.
        tablesWrap.appendChild(stateBlock({
          kind: "empty", title: "Fleet-wide relationships aren't available in this snapshot",
          msg: "Pick a repo to see its own relationship tables instead.",
          action: h("button", { class: "cl-btn cl-btn--primary", type: "button", onclick: function () { go("#/fleet"); } }, "Open fleet")
        }));
      }
    });
  }
  function graphSrc(scope, id) {
    var slug = id ? id.replace(/\//g, "__") : null;
    if (MODE === "static") return scope === "repo" && slug ? "graph/repo-" + slug + ".html" : "graph/overview.html";
    return scope === "repo" && slug ? "/graph/repo-" + slug : "/graph/overview";
  }

  // ---- Blast radius -----------------------------------------------------
  var blastCfg = { hops: 3, limit: 100, crossOnly: false, rels: { calls: true, depends_on: true } };
  function viewSymbol(seed) {
    if (!seed) {
      renderInto("symbol-body", stateBlock({
        kind: "empty", title: "Pick a symbol to trace impact",
        msg: "Search a symbol or click one in a repo, then trace what a change would touch.",
        action: h("button", { class: "cl-btn cl-btn--primary", type: "button", onclick: function () { go("#/search"); } }, "Search symbols")
      }));
      return;
    }
    ctx.nodeId = seed; ctx.symbolRepo = null; ctx.symbolExtras = null; ctx.symbolTicket = null; refreshChrome();
    // Always load the payload at the widest hops (3) so the slider/relation/cross
    // controls are pure client-side filters: narrowing re-paints in place (no re-fetch,
    // no skeleton flash, no rebuilt slider). Static already returns the hops=3
    // precompute; in live mode requesting max hops is what makes client narrowing correct.
    asyncPanel("symbol-body", function () { return CL.data.impact(seed, 3, blastCfg.limit); }, function (imp) {
      var body = h("div", { class: "cl-panel__body" });
      if (!imp.found) {
        if (imp.static_missing) {
          return stateBlock({
            kind: "unavailable", title: "Not precomputed in this snapshot",
            msg: "This static export ships a representative slice. Run the live server to trace any symbol with no caps.",
            cmd: "contextlake kb dashboard --serve",
            action: genAction("Run live server", "contextlake kb dashboard --serve")
          });
        }
        if (imp.ambiguous && imp.candidates && imp.candidates.length) {
          var amb = stateBlock({
            kind: "empty", title: "“" + seed + "” is defined in several repos",
            msg: "Pick the one you mean:"
          });
          imp.candidates.forEach(function (c) {
            amb.appendChild(h("button", { class: "cl-btn", type: "button",
              title: c.kind + " " + (c.name || seed) + " in " + c.repo,
              onclick: function () { go("#/repo/" + c.repo); } },
              kindIcon(c.kind), c.repo));
          });
          return amb;
        }
        return stateBlock({ kind: "empty", title: "Symbol not found",
          msg: "No node matched \"" + seed + "\"." });
      }
      body.appendChild(h("div", { class: "cl-card cl-sectionhead" },
        h("div", { class: "cl-row" }, kindIcon("function"), h("strong", null, imp.name || seed),
          citeButton({ claim: imp.name || seed, repo: imp.repo, source: imp.seed, confidence: "EXTRACTED" }))));

      // The impact payload now carries the seed's own repo (data.py), so "cross-repo
      // only" and the breadcrumb's repo-scoped links work in live mode too, not just
      // the static snapshot's precomputed symbol index.
      //
      // A seed that IS a shared node itself (an imported module, an HTTP endpoint, an
      // event topic -- reachable from any search hit's "Blast" button) reports a
      // pseudo-repo like "(shared)"/"(packages)"/"(external)", never a real repo id
      // (see kb/model.py:SHARED_REPO). Treat that the same as "repo unknown": a
      // #/repo/(shared) or #/arch/(shared) link would resolve to nothing.
      var seedRepo = (imp.repo && imp.repo.charAt(0) !== "(") ? imp.repo : null;
      var crossKnown = seedRepo != null;
      if (!crossKnown) blastCfg.crossOnly = false;
      // The Diagram breadcrumb only needs the repo id (known now); Wiki/Links need a
      // fetch to know whether either actually exists for this repo, so they append
      // once that resolves rather than blocking on it.
      ctx.symbolRepo = seedRepo;
      ctx.symbolTicket = (imp.ticket && imp.ticket.length) ? imp.ticket : null;
      refreshChrome("symbol");
      loadSymbolCrumbExtras(seedRepo, seed);

      // Lanes + summary live in their own container so a control change re-paints ONLY
      // this, leaving the controls (and any keyboard focus on the slider) untouched.
      var dynWrap = h("div");
      function visibleHits() {
        return (imp.hits || []).filter(function (hi) {
          if (hi.hop > blastCfg.hops) return false;
          if (!gtActive(hi.confidence)) return false;
          if (blastCfg.crossOnly && crossKnown && hi.repo === seedRepo) return false;
          if (blastCfg.rels.calls === false && hi.via === "calls") return false;
          if (blastCfg.rels.depends_on === false && hi.via === "depends_on") return false;
          return true;
        });
      }
      function repaint() {
        clear(dynWrap);
        var hits = visibleHits();
        var repos = {}; hits.forEach(function (hi) { repos[hi.repo] = 1; });
        dynWrap.appendChild(h("p", { role: "status" },
          "Changing this touches " + Object.keys(repos).length + " repos, " + hits.length + " symbols. " +
          hits.filter(function (x) { return x.confidence === "INFERRED"; }).length + " paths are inferred — treat as possible, not certain."));
        if (imp.truncated) dynWrap.appendChild(h("div", { class: "cl-truncbanner" }, "Showing first " + (imp.total || hits.length) + " — narrow relations or hops to see fewer."));
        if (!hits.length) { dynWrap.appendChild(stateBlock({ kind: "empty", title: "No downstream dependents", msg: "This symbol is a leaf at these settings." })); return; }
        var lanes = h("div", { class: "cl-lanes" });
        [1, 2, 3].slice(0, blastCfg.hops).forEach(function (hop) {
          var lane = h("div", { class: "cl-lane" });
          lane.appendChild(h("div", { class: "cl-lane__head", id: "cl-lane-h" + hop }, "Hop " + hop));
          // real <ul>/<li>; the hits stay buttons (see repoCollection)
          var items = h("ul", { class: "cl-lane__items", "aria-labelledby": "cl-lane-h" + hop });
          hits.filter(function (hi) { return hi.hop === hop; }).forEach(function (hi) {
            items.appendChild(h("li", null, h("button", {
              type: "button", class: "cl-hit cl-hit--" + String(hi.confidence).toLowerCase(),
              onclick: function () { go("#/symbol/" + hi.id); }
            }, kindIcon(hi.kind),
              h("span", { class: "cl-hit__name" }, hi.name),
              h("span", { class: "cl-hit__via" }, hi.repo + " · via " + hi.via))));
          });
          lane.appendChild(items);
          lanes.appendChild(lane);
        });
        dynWrap.appendChild(lanes);
      }

      var controls = h("div", { class: "cl-card cl-row" },
        labelWrap("Hops", h("input", { type: "range", min: "1", max: "3", value: String(blastCfg.hops), oninput: function () { blastCfg.hops = +this.value; repaint(); } })),
        toggleBtn("calls", "calls", repaint), toggleBtn("depends_on", "depends_on", repaint),
        h("button", {
          class: "cl-btn", type: "button", disabled: !crossKnown,
          title: crossKnown ? "Show only impact escaping the seed's own repo" : "Needs a known seed repo (live server or the static slice's index)",
          "aria-pressed": String(blastCfg.crossOnly),
          onclick: function () { blastCfg.crossOnly = !blastCfg.crossOnly; this.setAttribute("aria-pressed", String(blastCfg.crossOnly)); repaint(); }
        }, "Cross-repo only"));
      body.appendChild(controls);
      body.appendChild(dynWrap);
      repaint();

      // Call sequence: single-seed Mermaid sequenceDiagram (kb/visualize/diagrams.py
      // ::to_sequence_diagram, served via data.py's sequence_diagram()) -- doesn't fit
      // the repo-wide Diagrams tab (DIAGRAM_FORMATS deliberately excludes it), so it
      // lives here, fetched once per seed via imp.seed (already resolved by impact()).
      var seqWrap = h("div", { class: "cl-card" }, h("strong", null, "Call sequence"));
      body.appendChild(seqWrap);
      if (MODE === "static") {
        seqWrap.appendChild(stateBlock({
          kind: "unavailable", title: "Sequence diagram is live-only",
          msg: "This static export has no running server behind it.",
          cmd: "contextlake kb dashboard --serve",
          action: genAction("Run live server", "contextlake kb dashboard --serve")
        }));
      } else {
        var seqBody = h("div"); seqWrap.appendChild(seqBody);
        seqBody.appendChild(skeleton(1));
        CL.data.sequenceDiagram(imp.seed).then(function (res) {
          clear(seqBody);
          if (res.error) { seqBody.appendChild(stateBlock({ kind: "empty", title: "No call sequence for this symbol" })); return; }
          seqBody.appendChild(mermaidCard(res.text));
        }).catch(function (e) {
          clear(seqBody);
          seqBody.appendChild(stateBlock({ kind: "error", title: "Couldn't load call sequence", msg: String(e) }));
        });
      }

      return body;
    });
  }
  function toggleBtn(rel, label, onChange) {
    return h("button", { class: "cl-btn", type: "button", "aria-pressed": String(blastCfg.rels[rel] !== false), onclick: function () { blastCfg.rels[rel] = blastCfg.rels[rel] === false; this.setAttribute("aria-pressed", String(blastCfg.rels[rel] !== false)); if (onChange) onChange(); else CL.router.render(); } }, label);
  }
  function labelWrap(text, ctrl) { return h("label", { class: "cl-row" }, text, ctrl); }

  // ---- Path ---------------------------------------------------------------
  // "How does A reach B" as a route, not a diagram -- drawing the rest of the
  // graph around a single path only adds places to get lost. Reuses the same
  // resolve_target id/name/fuzzy resolution + ambiguous-candidates handling
  // the Blast radius view already has (kb/dashboard/data.py's path()).
  var pathState = { from: "", to: "" };
  function viewPath() {
    if (MODE === "static") { renderInto("path-body", stateBlock({
      kind: "unavailable", title: "Path is live-only",
      msg: "This static export has no running server behind it.",
      cmd: "contextlake kb dashboard --serve",
      action: genAction("Run live server", "contextlake kb dashboard --serve")
    })); return; }
    var body = h("div", { class: "cl-panel__body" });
    body.appendChild(h("p", { class: "cl-muted" },
      "Find the shortest route between two symbols over the real call graph."));
    var fromInput = h("input", { class: "cl-pathinput", type: "text", value: pathState.from,
      placeholder: "from symbol or node id", "aria-label": "From symbol" });
    var toInput = h("input", { class: "cl-pathinput", type: "text", value: pathState.to,
      placeholder: "to symbol or node id", "aria-label": "To symbol" });
    var resultWrap = h("div");
    function renderResult(res) {
      clear(resultWrap);
      if (!res.found) {
        if (res.ambiguous && res.candidates && res.candidates.length) {
          var amb = stateBlock({
            kind: "empty",
            title: "“" + (res.which === "from" ? res.from : res.to) + "” is defined in several repos",
            msg: "Narrow to one repo and try again:"
          });
          res.candidates.forEach(function (c) {
            amb.appendChild(h("div", { class: "cl-row" }, kindIcon(c.kind), c.name, h("span", { class: "cl-muted" }, c.repo)));
          });
          resultWrap.appendChild(amb);
          return;
        }
        resultWrap.appendChild(stateBlock({ kind: "empty", title: "No path found",
          msg: "No route between “" + res.from + "” and “" + res.to + "” within the hop limit." }));
        return;
      }
      var steps = h("div", { class: "cl-pathsteps" });
      res.steps.forEach(function (s, i) {
        steps.appendChild(h("div", { class: "cl-row" },
          h("span", { class: "cl-stepnum" }, String(i + 1)), kindIcon(s.kind), h("strong", null, s.name),
          s.file ? h("span", { class: "cl-muted" }, s.file) : null,
          h("button", { class: "cl-btn", type: "button", onclick: function () { go("#/symbol/" + s.id); } },
            h("span", { html: icon("ui-blast") }), "Blast radius")));
        if (i < res.steps.length - 1) steps.appendChild(h("div", { class: "cl-patharrow" }, "↓ calls"));
      });
      resultWrap.appendChild(steps);
    }
    function runSearch() {
      var from = fromInput.value.trim(), to = toInput.value.trim();
      if (!from || !to) return;
      pathState.from = from; pathState.to = to;
      clear(resultWrap); resultWrap.appendChild(skeleton(1));
      CL.data.path(from, to).then(renderResult).catch(function (e) {
        clear(resultWrap);
        resultWrap.appendChild(stateBlock({ kind: "error", title: "Couldn't find a path", msg: String(e) }));
      });
    }
    var form = h("form", { class: "cl-pathform", onsubmit: function (ev) { ev.preventDefault(); runSearch(); } },
      fromInput, h("span", { class: "cl-muted", "aria-hidden": "true" }, "→"), toInput,
      h("button", { class: "cl-btn cl-btn--primary", type: "submit" }, "Find path"));
    body.appendChild(form);
    body.appendChild(resultWrap);
    renderInto("path-body", body);
    if (pathState.from && pathState.to) runSearch();
  }

  // ---- Health -----------------------------------------------------------
  function viewHealth() {
    asyncPanel("health-body", CL.data.health, function (hd) {
      var body = h("div", { class: "cl-panel__body" });
      // `unreadable` counts alongside stale, never inside it: both are faults,
      // but only one of them is fixed by re-indexing, so the panel below has to
      // give them different advice. Without its own tile, a repo whose checkout
      // vanished used to show under "Stale repos" and, once lint stopped
      // miscounting it there, would have shown nowhere at all.
      var unreadable = hd.unreadable || 0;
      var clean = !hd.stale && !hd.dangling && !unreadable;
      body.appendChild(h("div", { class: "cl-statgrid" },
        statTile(hd.checked, "Checked"), statTile(hd.stale, "Stale repos"),
        statTile(unreadable, "Unreadable"), statTile(hd.dangling, "Dangling edges")));
      var ur = hd.unresolved;
      if (ur && ur.supported && ur.sites) {
        var uc2 = h("div", { class: "cl-card" },
          h("strong", null, "Unresolved references"),
          h("p", { class: "cl-muted" },
            num(ur.sites) + " reference" + (ur.sites === 1 ? "" : "s") +
            " the parser could not pin to one definition, across " + num(ur.edges) +
            " candidate edges. This is the parser saying so, not a fault to fix by " +
            "re-indexing. Ranked by name, because one disambiguation covers " +
            "every site that shares it."));
        uc2.appendChild(table(
          ["Name", "Sites", "Avg candidates", "First seen at"],
          ur.names.map(function (n) {
            return [n.name, num(n.sites), String(n.candidates), n.example];
          })));
        body.appendChild(uc2);
      }
      if (clean) { body.appendChild(stateBlock({ kind: "ok", title: "Clear water", msg: "No stale repos, no dangling edges." })); return body; }
      if (hd.stale_repos && hd.stale_repos.length) {
        var sc = h("div", { class: "cl-card" }, h("strong", null, "Stale repos"));
        hd.stale_repos.forEach(function (r) {
          sc.appendChild(h("div", { class: "cl-row" },
            h("button", { class: "cl-btn", type: "button", onclick: function () { go("#/repo/" + r); } }, r),
            h("span", { class: "cl-healthchip cl-healthchip--stale" }, "HEAD moved"),
            h("code", null, "contextlake kb index")));
        });
        body.appendChild(sc);
      }
      if (hd.unreadable_repos && hd.unreadable_repos.length) {
        var uc = h("div", { class: "cl-card" }, h("strong", null, "Unreadable repos"));
        hd.unreadable_repos.forEach(function (r) {
          uc.appendChild(h("div", { class: "cl-row" },
            h("button", { class: "cl-btn", type: "button", onclick: function () { go("#/repo/" + r); } }, r),
            h("span", { class: "cl-healthchip cl-healthchip--dangling" }, "no checkout"),
            h("span", { class: "cl-muted" }, "re-clone it, or drop it from the store")));
        });
        body.appendChild(uc);
      }
      if (hd.dangling_sample && hd.dangling_sample.length) {
        body.appendChild(table(["Repo", "Source", "Relation", "Missing target"],
          hd.dangling_sample.map(function (d) { return [d.repo, d.src, d.relation, d.dst]; })));
      }
      return body;
    });
  }
  function statTile(n, cap) { return h("div", { class: "cl-stat" }, h("div", { class: "cl-stat__num" }, num(n)), h("div", { class: "cl-stat__cap" }, cap)); }
  // Same tile, for a non-numeric value (e.g. "On"/"Off") -- statTile always
  // runs its value through num()'s Number(n).toLocaleString(), which renders
  // "NaN" for anything that isn't actually numeric.
  function textTile(text, cap) { return h("div", { class: "cl-stat" }, h("div", { class: "cl-stat__num" }, text), h("div", { class: "cl-stat__cap" }, cap)); }

  // ---- Search -----------------------------------------------------------
  var searchState = { mode: "symbols", scope: "all", q: "" };
  function viewSearch(q) {
    searchState.q = q || searchState.q;
    var body = clear($("#search-body"));
    var seg = h("div", { class: "cl-modeseg", role: "group", "aria-label": "Search mode" },
      h("button", { type: "button", "aria-pressed": String(searchState.mode === "symbols"), onclick: function () { searchState.mode = "symbols"; runSearch(); } }, "Symbols"),
      h("button", { type: "button", "aria-pressed": String(searchState.mode === "semantic"), onclick: function () { searchState.mode = "semantic"; runSearch(); } }, "Semantic"));
    var field = h("input", { type: "search", class: "cl-searchfield", id: "cl-searchfield", placeholder: "Search symbols across the fleet", value: searchState.q, "aria-label": "Search" });
    var scopeBtn = h("button", { class: "cl-btn", type: "button", "aria-pressed": String(searchState.scope === "repo"), onclick: function () { searchState.scope = searchState.scope === "repo" ? "all" : "repo"; runSearch(); } });
    function paintScope() { scopeBtn.textContent = searchState.scope === "repo" && ctx.repoId ? "Scoped: " + ctx.repoId : "All repos"; scopeBtn.setAttribute("aria-pressed", String(searchState.scope === "repo")); }
    paintScope();
    var results = h("div", { class: "cl-panel__body", "aria-live": "polite" });
    body.appendChild(h("div", { class: "cl-row" }, seg, scopeBtn));
    body.appendChild(field); body.appendChild(results);

    function runSearch() {
      seg.querySelectorAll("button").forEach(function (b, i) { b.setAttribute("aria-pressed", String((i === 0) === (searchState.mode === "symbols"))); });
      paintScope();
      var q = field.value.trim(); searchState.q = q;
      clear(results);
      if (searchState.mode === "semantic" && MODE === "static") {
        results.appendChild(stateBlock({ kind: "unavailable", title: "Semantic search is live-only", msg: "Needs the running server.", cmd: "contextlake kb serve" }));
        return;
      }
      if (!q) { results.appendChild(stateBlock({ kind: "empty", title: "Search symbols across the fleet" })); return; }
      results.appendChild(skeleton(2));
      var repo = searchState.scope === "repo" ? ctx.repoId : null;
      CL.data.search(q, null, repo).then(function (res) {
        clear(results);
        if (!res.results.length) { results.appendChild(stateBlock({ kind: "empty", title: "No symbols match \"" + q + "\"" })); return; }
        if (searchState.mode === "semantic" && !res.semantic) results.appendChild(h("p", { class: "cl-muted" }, "Semantic unavailable — showing lexical matches."));
        res.results.forEach(function (n) {
          // Two SIBLING buttons in a plain row, never a button inside a button --
          // the outer control used to absorb "Blast" into its own accessible name
          // and advertise an action it does not perform (WCAG 4.1.2).
          results.appendChild(h("div", { class: "cl-result" },
            h("button", {
              type: "button", class: "cl-result__main",
              onclick: function () { go("#/repo/" + n.repo + "?tab=anatomy"); }
            }, kindIcon(n.kind),
              h("span", null, h("strong", null, n.qualified_name || n.name),
                h("div", { class: "cl-result__meta" }, n.repo + (n.file ? " · " + n.file + (n.line ? ":" + n.line : "") : "")))),
            h("button", {
              type: "button", class: "cl-btn",
              "aria-label": "Blast radius for " + (n.qualified_name || n.name),
              onclick: function () { go("#/symbol/" + (n.id || n.name)); }
            }, "Blast")));
        });
      }).catch(function (e) { clear(results); results.appendChild(stateBlock({ kind: "error", title: "Search failed", msg: String(e.message || e) })); });
    }
    field.addEventListener("input", debounce(runSearch, 200));
    field.focus();
    if (searchState.q) runSearch();
  }

  // ---- Chat ---------------------------------------------------------------
  // Always-on free layer: the same deterministic `ask` router `contextlake
  // serve` exposes over MCP, reached in-process (no logic duplicated). LLM
  // prose is layered on top only when the dashboard was started with
  // --llm-chat (window.__CL_LLM_CHAT__) -- an opt-in made once at server
  // start, never toggled per-question here.
  var chatHistory = [];
  function renderChatNode(n) {
    return h("div", { class: "cl-result", style: "cursor:default" }, kindIcon(n.kind),
      h("span", null, h("strong", null, n.qualified_name || n.name),
        h("div", { class: "cl-result__meta" }, n.repo + (n.file ? " · " + n.file + (n.line_start ? ":" + n.line_start : "") : ""))));
  }
  function renderChatStructured(s) {
    var box = h("div", { class: "cl-card" });
    if (s.nodes && s.nodes.length) {
      s.nodes.forEach(function (n) { box.appendChild(renderChatNode(n)); });
      if (s.truncated) box.appendChild(h("p", { class: "cl-muted" }, "More results exist than shown."));
    } else if (s.blast) {
      box.appendChild(h("p", null, h("strong", null, s.blast.total), " node(s) within ", s.blast.hops, " hop(s)"));
      (s.blast.hits || []).forEach(function (n) {
        box.appendChild(h("div", { class: "cl-result", style: "cursor:default" }, kindIcon(n.kind),
          h("span", null, h("strong", null, n.name), h("div", { class: "cl-result__meta" }, n.repo + " · " + n.hop + " hop via " + n.via + " · " + n.confidence))));
      });
      if (s.blast.truncated) box.appendChild(h("p", { class: "cl-muted" }, "More results exist than shown."));
    } else if (s.owners && s.owners.owners) {
      s.owners.owners.forEach(function (o) {
        box.appendChild(h("div", { class: "cl-result", style: "cursor:default" },
          h("span", null, h("strong", null, o.name), h("div", { class: "cl-result__meta" }, o.commits + " commit(s), last active " + o.last_active))));
      });
    } else if (s.wiki && s.wiki.found) {
      if (s.wiki.stale) box.appendChild(h("p", { class: "cl-muted" }, "STALE -- the code changed since this was generated."));
      box.appendChild(h("pre", { class: "cl-snippet" }, s.wiki.markdown || ""));
    } else if (s.brief && s.brief.found) {
      box.appendChild(h("pre", { class: "cl-snippet" }, JSON.stringify(s.brief, null, 2)));
    } else {
      box.appendChild(h("p", { class: "cl-muted" }, "No results."));
    }
    return box;
  }
  function viewChat() {
    if (MODE === "static") { liveOnlyBlock("chat-body", "Chat is live-only"); return; }
    var body = clear($("#chat-body"));
    var llmOn = !!window.__CL_LLM_CHAT__;
    body.appendChild(h("p", { class: "cl-muted" },
      llmOn ? "Answers are LLM-synthesized prose, grounded in cited graph data (shown below each answer)."
            : "Answers are the free graph router -- structured and cited, not written prose. Start the dashboard with --llm-chat for prose answers."));
    var history = h("div", { class: "cl-panel__body", id: "chat-history", "aria-live": "polite" });
    var field = h("input", { type: "text", class: "cl-searchfield", placeholder: "Ask about the fleet -- \"who calls X\", \"what depends on Y\", \"explain repo Z\"", "aria-label": "Question" });
    var askBtn = h("button", { class: "cl-btn cl-btn--primary", type: "button" }, "Ask");
    body.appendChild(h("div", { class: "cl-row" }, field, askBtn));
    body.appendChild(history);
    chatHistory.forEach(function (turn) { history.appendChild(turn); });

    function send(q) {
      field.disabled = true; askBtn.disabled = true;
      var turn = h("div", { class: "cl-card" });
      turn.appendChild(h("p", null, h("strong", null, "You: "), q));
      var pending = skeleton(1);
      turn.appendChild(pending);
      history.appendChild(turn);
      chatHistory.push(turn);
      turn.scrollIntoView({ block: "nearest" });
      CL.data.chat(q).then(function (res) {
        turn.removeChild(pending);
        if (res.llm_used) {
          turn.appendChild(h("p", null, h("strong", null, "Answer: "), res.answer));
          var details = h("details", null, h("summary", null, "Graph data this answer is grounded in"), renderChatStructured(res.structured));
          turn.appendChild(details);
        } else {
          if (res.llm_error) turn.appendChild(h("p", { class: "cl-muted" }, "LLM synthesis unavailable (", res.llm_error, ") -- showing the free router result."));
          turn.appendChild(h("p", null, res.structured.note));
          turn.appendChild(renderChatStructured(res.structured));
        }
      }).catch(function (e) {
        turn.removeChild(pending);
        var retryBtn = h("button", { class: "cl-btn", type: "button" }, "Retry");
        retryBtn.addEventListener("click", function () {
          var i = chatHistory.indexOf(turn);
          if (i !== -1) chatHistory.splice(i, 1);
          history.removeChild(turn);
          send(q);
        });
        turn.appendChild(stateBlock({ kind: "error", title: "Question failed", msg: String(e.message || e), action: retryBtn }));
      }).then(function () {
        field.disabled = false; askBtn.disabled = false; field.focus();
      });
    }
    function ask() {
      var q = field.value.trim();
      if (!q) return;
      field.value = "";
      send(q);
    }
    askBtn.addEventListener("click", ask);
    field.addEventListener("keydown", function (e) { if (e.key === "Enter") ask(); });
    field.focus();
  }

  // ---- MCP console + Settings --------------------------------------------
  // Both describe this machine/process (the running server, the active
  // kb.toml) rather than the graph, so neither has anything meaningful to
  // show from an offline --site snapshot -- same live-only treatment
  // viewSearch already gives semantic mode.
  function liveOnlyBlock(bodyId, title) {
    renderInto(bodyId, stateBlock({
      kind: "unavailable", title: title,
      msg: "This static export has no running server behind it.",
      cmd: "contextlake kb dashboard --serve",
      action: genAction("Run live server", "contextlake kb dashboard --serve")
    }));
  }
  function copyCard(title, text) {
    var card = h("div", { class: "cl-card" });
    var copyBtn = h("button", { class: "cl-btn", type: "button" }, h("span", { html: icon("ui-copy") }), " Copy");
    copyBtn.addEventListener("click", function () {
      try { navigator.clipboard.writeText(text); live("Copied " + title); }
      catch (e) { /* clipboard blocked under file:// */ }
      copyBtn.lastChild.nodeValue = " Copied";
    });
    card.appendChild(h("div", { class: "cl-row" }, h("strong", null, title), copyBtn));
    card.appendChild(h("pre", { class: "cl-snippet" }, text));
    return card;
  }
  function viewMcp() {
    if (MODE === "static") { liveOnlyBlock("mcp-body", "MCP console is live-only"); return; }
    asyncPanel("mcp-body", CL.data.mcp, function (d) {
      var body = h("div", { class: "cl-panel__body" });
      body.appendChild(h("div", { class: "cl-statgrid" },
        statTile(d.tool_count, "Tools exposed"),
        textTile(d.semantic_search_available ? "On" : "Off", "Semantic search")));
      body.appendChild(copyCard(".mcp.json", JSON.stringify(d.mcp_json, null, 2)));
      body.appendChild(copyCard(".vscode/mcp.json", JSON.stringify(d.vscode_mcp_json, null, 2)));
      var tc = h("div", { class: "cl-card" }, h("strong", null, "Tool catalog (" + d.tool_count + ")"));
      d.tools.forEach(function (t) {
        tc.appendChild(h("div", { class: "cl-row" },
          h("code", null, t.name),
          h("span", { class: "cl-muted" }, (t.description || "").split("\n")[0])));
      });
      body.appendChild(tc);
      if (MUTATIONS) body.appendChild(mcpServerCard(d.http_server || { running: false }));
      return body;
    });
  }
  // The HTTP-transport MCP server this card controls is a *separate* process from
  // the one serving this dashboard -- typically an editor spawns `contextlake serve`
  // itself over stdio, which this card cannot see or manage. This is specifically
  // for `contextlake serve --transport http`, tracked via a pidfile next to the store.
  function mcpServerCard(status) {
    var card = h("div", { class: "cl-card" },
      h("strong", null, "HTTP-transport MCP server"),
      h("p", { class: "cl-muted" },
        "A separate ", h("code", null, "contextlake kb serve --transport http"),
        " process this dashboard can start/stop -- not the stdio server your editor spawns."));
    var row = h("div", { class: "cl-row" },
      h("span", null, "Status"),
      h("span", null, status.running
        ? ("Running — pid " + status.pid + " on " + status.host + ":" + status.port)
        : "Stopped"));
    card.appendChild(row);
    // The HTTP transport requires Authorization: Bearer <token>. The server was
    // spawned with its stderr discarded, so this card is the only place that
    // token is ever shown -- omitting it would leave a "Running" server no
    // client could actually connect to.
    // Endpoint shown scheme-less on purpose: the static-site export asserts
    // dashboard.js contains no absolute URL at all (nothing may be fetched off
    // the machine), and the card's own text already says this is the plain-HTTP
    // transport. The /mcp path is the part worth stating -- the bare root 404s.
    if (status.running && status.token) {
      card.appendChild(h("div", { class: "cl-row" },
        h("span", null, "Endpoint (http)"),
        h("code", null, status.host + ":" + status.port + "/mcp")));
      card.appendChild(h("div", { class: "cl-row" },
        h("span", null, "Bearer token"),
        h("code", null, status.token)));
    }
    var actions = h("div", { class: "cl-actions" });
    function act(label, action, confirmMsg) {
      var btn = h("button", { class: "cl-btn", type: "button" }, label);
      btn.addEventListener("click", function () {
        if (!window.confirm(confirmMsg)) return;
        btn.disabled = true;
        CL.data.mcpServe(action).then(function () { viewMcp(); })
          .catch(function (e) { window.alert("Failed: " + e.message); btn.disabled = false; });
      });
      actions.appendChild(btn);
    }
    if (status.running) {
      act("Stop", "stop", "Stop the HTTP MCP server (pid " + status.pid + ")?");
      act("Restart", "restart", "Restart the HTTP MCP server?");
    } else {
      act("Start", "start", "Start an HTTP-transport MCP server on 127.0.0.1:8766?");
    }
    card.appendChild(actions);
    return card;
  }
  // A "Regenerate wiki" control: single-repo (repoId given) or fleet-wide
  // (repoId omitted). Estimate-then-confirm before starting -- a fleet-wide
  // run with Force can fire an LLM call per indexed repo, so the count is
  // shown up front rather than discovered after the fact. Polls status with
  // a setTimeout chain (never overlapping requests); wikiPollGen guards
  // against a stale chain updating a card the user has since navigated away
  // from, same idiom as the Diagrams tab's renderGen.
  var wikiPollGen = 0;
  function wikiRegenerateCard(repoId) {
    var myGen = ++wikiPollGen;
    var card = h("div", { class: "cl-card" },
      h("strong", null, "Regenerate wiki" + (repoId ? "" : " — fleet-wide")),
      h("p", { class: "cl-muted" },
        repoId ? "Runs contextlake kb wiki for this repo only."
              : "Runs contextlake kb wiki for every indexed repo — skips repos already up to date unless Force is checked."));
    var statusLine = h("div", { class: "cl-row" }, h("span", null, "Idle"));
    var forceCheck = h("input", { type: "checkbox" });
    var logBox = h("pre", { class: "cl-snippet", hidden: true });
    var btn = h("button", { class: "cl-btn cl-btn--primary", type: "button" },
      "Regenerate" + (repoId ? "" : " (fleet-wide)"));
    card.appendChild(statusLine);
    card.appendChild(h("label", { class: "cl-row" }, forceCheck, "Force (ignore freshness — regenerate every targeted repo)"));
    card.appendChild(h("div", { class: "cl-actions" }, btn));
    card.appendChild(logBox);

    function setStatus(text, running) {
      clear(statusLine);
      statusLine.appendChild(h("span", null, text));
      btn.disabled = running;
    }
    function poll() {
      if (myGen !== wikiPollGen) return;
      CL.data.wikiStatus().then(function (s) {
        if (myGen !== wikiPollGen) return;
        if (s.log_tail) { logBox.hidden = false; logBox.textContent = s.log_tail; }
        if (s.running) {
          setStatus("Running — pid " + s.pid + (s.repo ? " (" + s.repo + ")" : " (fleet-wide)"), true);
          setTimeout(poll, 2000);
        } else {
          setStatus(s.finished ? "Finished — see log below" : "Idle", false);
        }
      }).catch(function () { /* static mode / not yet started -- leave as Idle */ });
    }
    btn.addEventListener("click", function () {
      btn.disabled = true;
      CL.data.wikiEstimate(repoId, forceCheck.checked).then(function (est) {
        if (est.total === 0) { window.alert("No indexed repos match this scope."); btn.disabled = false; return null; }
        var msg = forceCheck.checked
          ? ("This bypasses the freshness check — all " + est.total + " repo(s) will regenerate. Continue?")
          : (est.would_regenerate + " of " + est.total + " repo(s) will regenerate (" +
            est.unchanged + " already up to date). Continue?");
        if (!window.confirm(msg)) { btn.disabled = false; return null; }
        return CL.data.wikiGenerate(repoId, forceCheck.checked).then(function (r) {
          if (!r.ok) { window.alert("Failed: " + (r.error || "unknown error")); btn.disabled = false; return; }
          poll();
        });
      }).catch(function (e) { window.alert("Failed: " + e.message); btn.disabled = false; });
    });
    poll();   // reflect an already-in-progress run on load
    return card;
  }
  function fmtBytes(n) {
    if (n == null) return "—";
    var units = ["B", "KB", "MB", "GB", "TB"], i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + " " + units[i];
  }
  function viewSettings() {
    if (MODE === "static") { liveOnlyBlock("settings-body", "Settings is live-only"); return; }
    asyncPanel("settings-body", CL.data.settings, function (d) {
      var body = h("div", { class: "cl-panel__body" });
      body.appendChild(h("div", { class: "cl-statgrid" },
        textTile(fmtBytes(d.store_size_bytes), "Store size"),
        statTile(d.schema_version.running, "Schema version"),
        statTile(d.sources.length, "Connectors")));

      var storeCard = h("div", { class: "cl-card" }, h("strong", null, "Store"));
      storeCard.appendChild(h("div", { class: "cl-row" }, h("span", null, "Path"), h("code", null, d.store_dir)));
      if (d.mirror_root) storeCard.appendChild(h("div", { class: "cl-row" }, h("span", null, "Mirror root"), h("code", null, d.mirror_root)));
      body.appendChild(storeCard);

      var embCard = h("div", { class: "cl-card" }, h("strong", null, "Embeddings"));
      embCard.appendChild(h("div", { class: "cl-row" }, h("span", null, "Enabled"), h("span", null, d.embeddings.enabled ? "Yes" : "No")));
      if (d.embeddings.enabled) {
        embCard.appendChild(h("div", { class: "cl-row" }, h("span", null, "Provider"), h("code", null, d.embeddings.provider)));
        if (d.embeddings.model) embCard.appendChild(h("div", { class: "cl-row" }, h("span", null, "Model"), h("code", null, d.embeddings.model)));
      }
      body.appendChild(embCard);

      var llmCard = h("div", { class: "cl-card" }, h("strong", null, "LLM (wiki generation)"));
      llmCard.appendChild(h("div", { class: "cl-row" }, h("span", null, "Enabled"), h("span", null, d.llm.enabled ? "Yes" : "No")));
      if (d.llm.enabled) llmCard.appendChild(h("div", { class: "cl-row" }, h("span", null, "Provider"), h("code", null, d.llm.provider)));
      body.appendChild(llmCard);

      if (MUTATIONS) body.appendChild(wikiRegenerateCard(null));

      if (d.sources.length) {
        body.appendChild(table(["Name", "Type", "Enabled"],
          d.sources.map(function (s) { return [s.name, s.type, s.enabled ? "Yes" : "No"]; })));
      } else {
        body.appendChild(stateBlock({ kind: "empty", title: "No connectors configured", cmd: "contextlake kb source add <name> --type <type>" }));
      }
      body.appendChild(h("p", { class: "cl-muted" }, "Read-only — edit ", h("code", null, "kb.toml"), " directly to change any of this."));
      return body;
    });
  }

  // ---- generic table (with pagination) ----------------------------------
  function num(n) {
    if (n == null) return "—";
    try { return Number(n).toLocaleString(); } catch (e) { return String(n); }
  }
  function table(headers, rows, numCols, ambFlags) {
    var wrap = h("div", { class: "cl-tablewrap" });
    var t = h("table", { class: "cl-table" });
    var thead = h("thead"), htr = h("tr");
    headers.forEach(function (hd, i) { htr.appendChild(h("th", { scope: "col", class: numCols && numCols[i] ? "cl-num" : null }, hd)); });
    thead.appendChild(htr); t.appendChild(thead);
    var tbody = h("tbody"); t.appendChild(tbody);
    var PAGE = 60, shown = 0;
    function addPage() {
      var end = Math.min(shown + PAGE, rows.length);
      for (; shown < end; shown++) {
        var tr = h("tr", ambFlags && ambFlags[shown] ? { class: "cl-amb" } : null);
        rows[shown].forEach(function (cell, i) {
          tr.appendChild(h(i === 0 ? "th" : "td", { scope: i === 0 ? "row" : null, class: numCols && numCols[i] ? "cl-num" : null }, cell));
        });
        tbody.appendChild(tr);
      }
    }
    addPage(); wrap.appendChild(t);
    if (rows.length > PAGE) {
      var more = h("button", { class: "cl-btn cl-more", type: "button" }, "Show more (" + (rows.length - shown) + ")");
      more.addEventListener("click", function () { addPage(); if (shown >= rows.length) more.remove(); else more.textContent = "Show more (" + (rows.length - shown) + ")"; });
      wrap.appendChild(more);
    }
    return wrap;
  }

  // ===================================================================== //
  // ROUTER + CHROME                                                        //
  // ===================================================================== //
  var PANELS = ["fleet", "repo", "arch", "symbol", "path", "health", "search", "chat", "mcp", "settings"];
  // Track the last-rendered route so we only move focus to #app on an actual
  // route/lens CHANGE (navigation), never on in-view data re-renders — e.g. the
  // ground-truth filter, trust-bar segments and blast toggles all call
  // CL.router.render() with an unchanged hash, and stealing focus there breaks
  // WCAG 2.4.3 (focus order). Tab switches change the hash, so they do refocus.
  // `hasRenderedOnce` guards the OTHER end of the same rule: `lastRouteSig`
  // starts null, so the very first render's sig always differs from it and used
  // to fire focus() on initial page load too -- stealing focus from the top of
  // the document (and the skip link) before the user has tabbed anywhere. Only
  // a render that follows an already-rendered route counts as a "navigation".
  var lastRouteSig = null;
  var hasRenderedOnce = false;
  function go(hash) { if (location.hash === hash) CL.router.render(); else location.hash = hash; }
  function parseHash() {
    var raw = location.hash.replace(/^#/, "") || "/fleet";
    var qi = raw.indexOf("?");
    var path = qi >= 0 ? raw.slice(0, qi) : raw;
    var query = {};
    if (qi >= 0) raw.slice(qi + 1).split("&").forEach(function (p) { var kv = p.split("="); query[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || ""); });
    var segs = path.split("/").filter(Boolean);
    return { lens: segs[0] || "fleet", rest: segs.slice(1).map(decodeURIComponent).join("/"), query: query };
  }
  CL.router = {
    render: function () {
      var r = parseHash();
      var lens = PANELS.indexOf(r.lens) >= 0 ? r.lens : "fleet";
      // map symbol alias
      if (r.lens === "impact") lens = "symbol";
      PANELS.forEach(function (p) { $("#panel-" + p).hidden = (p !== lens); });
      document.querySelectorAll(".cl-rail__item[data-lens]").forEach(function (a) {
        a.setAttribute("aria-current", a.dataset.lens === lens ? "page" : "false");
      });
      var sig = lens + "|" + r.rest + "|" + JSON.stringify(r.query);
      if (sig !== lastRouteSig) {
        lastRouteSig = sig;
        if (hasRenderedOnce) $("#app").focus({ preventScroll: false });
        hasRenderedOnce = true;
      }
      if (lens === "fleet") viewFleet();
      else if (lens === "repo") viewRepo(r.rest || ctx.repoId, r.query.tab);
      else if (lens === "arch") viewArch(r.rest || null);
      else if (lens === "symbol") viewSymbol(r.rest || ctx.nodeId);
      else if (lens === "path") viewPath();
      else if (lens === "health") viewHealth();
      else if (lens === "search") { if (r.query.q) searchState.q = r.query.q; viewSearch(searchState.q); }
      else if (lens === "chat") viewChat();
      else if (lens === "mcp") viewMcp();
      else if (lens === "settings") viewSettings();
      refreshChrome(lens);
    }
  };

  function refreshChrome(lens) {
    var ol = clear($("#cl-crumbs"));
    // openUrl (optional): an external link crumb (e.g. a confirmed Jira ticket)
    // opens its real URL in a new tab instead of an internal #/ navigation.
    function crumb(label, hash, current, openUrl) {
      var li = h("li");
      li.appendChild(h("button", { class: "cl-crumb", type: "button", "aria-current": current ? "page" : null, onclick: function () {
        if (openUrl) { window.open(openUrl, "_blank", "noopener"); return; }
        if (hash) go(hash);
      } }, label));
      ol.appendChild(li);
    }
    crumb("Lake", "#/fleet", lens === "fleet");
    if (ctx.repoId) {
      if (ctx.repoId.indexOf("/") >= 0) crumb(ctx.repoId.split("/")[0], "#/fleet");
      crumb(ctx.repoId, "#/repo/" + ctx.repoId, lens === "repo");
    }
    if (ctx.nodeId && lens === "symbol") {
      // The symbol's own repo path (acme / acme/auth-service) -- shown even when this
      // symbol was reached directly (search, a deep link) rather than by clicking
      // through its repo first, which is the only way ctx.repoId above gets set. Skipped
      // when it's already the pinned repoId's path, so it's never rendered twice.
      if (ctx.symbolRepo && ctx.symbolRepo !== ctx.repoId) {
        if (ctx.symbolRepo.indexOf("/") >= 0) crumb(ctx.symbolRepo.split("/")[0], "#/fleet");
        crumb(ctx.symbolRepo, "#/repo/" + ctx.symbolRepo, false);
      }
      crumb(String(ctx.nodeId).split("/").pop(), "#/symbol/" + ctx.nodeId, true);
      // Diagram/Wiki/Links: quick links onward from this symbol, not "you are here"
      // segments, so none of them carry aria-current. Diagram only needs the repo id
      // (known as soon as the impact payload resolves); Wiki/Links need to know the
      // repo actually HAS one, so they only appear once that's confirmed -- an absent
      // wiki or connector link is omitted, never shown as a dead/disabled crumb.
      if (ctx.symbolRepo) {
        crumb("Diagram", "#/arch/" + ctx.symbolRepo, false);
        var extras = ctx.symbolExtras;
        if (extras && extras.repo === ctx.symbolRepo) {
          if (extras.wiki) crumb("Wiki", "#/repo/" + ctx.symbolRepo + "?tab=wiki", false);
          if (extras.links) crumb("Links", "#/repo/" + ctx.symbolRepo + "?tab=links", false);
        }
      }
      // Ticket: per-symbol attribution (docstring/git-blame issue key, live-JQL
      // confirmed -- see connectors/symbol_refs.py), distinct from the repo-level
      // Links crumb above. Arrives with the impact payload itself, no extra
      // fetch needed. Opens the real tracker URL directly, same as clicking
      // through a search-result citation.
      if (ctx.symbolTicket && ctx.symbolTicket.length) {
        var tk = ctx.symbolTicket[0];
        crumb("Ticket" + (tk.name ? " " + tk.name : ""), null, false, tk.url || null);
      }
    }
    // pinned chip
    var pin = $("#cl-pinchip");
    if (ctx.repoId) {
      pin.hidden = false; clear(pin);
      pin.appendChild(h("span", { html: icon("ui-pin") }));
      pin.appendChild(document.createTextNode(ctx.repoId));
      pin.onclick = function () { ctx.repoId = null; ctx.nodeId = null; ctx.symbolRepo = null; ctx.symbolExtras = null; ctx.symbolTicket = null; refreshChrome(); live("Context cleared"); };
      pin.setAttribute("aria-label", "Clear pinned " + ctx.repoId);
    } else pin.hidden = true;
  }

  // Fetches whether the symbol's repo has a wiki / connector links, so the
  // breadcrumb can append Wiki/Links crumbs (or not) once known. Guarded against
  // the user navigating to a different symbol before this resolves.
  function loadSymbolCrumbExtras(repo, forSeed) {
    if (!repo) return;
    CL.data.repo(repo).then(function (d) {
      if (ctx.nodeId !== forSeed) return;  // navigated away; stale response, drop it
      ctx.symbolExtras = {
        repo: repo,
        wiki: !!(d.wiki && d.wiki.found),
        links: !!(d.links && Object.keys(d.links).length),
      };
      refreshChrome("symbol");
    }, function () {
      // repo() failed (e.g. static snapshot doesn't carry this repo's detail) --
      // Diagram still works from symbolRepo alone; Wiki/Links just stay omitted.
    });
  }

  // ---- command palette --------------------------------------------------
  var palSel = 0, palItems = [], palInvoker = null;
  function openPalette() {
    palInvoker = document.activeElement;
    var wrap = $("#cl-palette-wrap"); wrap.hidden = false;
    var input = $("#cl-palette-input"); input.value = ""; input.focus();
    paintPalette("");
  }
  function closePalette() {
    var wrap = $("#cl-palette-wrap");
    if (wrap.hidden) return;
    wrap.hidden = true;
    if (palInvoker && palInvoker.focus) { try { palInvoker.focus(); } catch (e) { } }
    palInvoker = null;
  }
  function paintPalette(q) {
    var list = clear($("#cl-palette-list")); palItems = []; palSel = 0;
    var ql = q.toLowerCase();
    var actions = [
      { g: "Go", label: "Fleet overview", hash: "#/fleet" },
      { g: "Go", label: "Health", hash: "#/health" },
      { g: "Go", label: "Search", hash: "#/search" },
      { g: "Go", label: "Architecture", hash: "#/arch" }
    ];
    var repos = [];
    if (MODE === "static" && SNAP.overview) repos = SNAP.overview.repos.map(function (r) { return { g: "Repo", label: r.id, hash: "#/repo/" + r.id }; });
    var syms = (CL.data.symbols() || []).slice(0, 300).map(function (s) { return { g: "Symbol", label: s.name + " · " + s.repo, hash: "#/symbol/" + s.id }; });
    var all = actions.concat(repos, syms).filter(function (x) { return !ql || x.label.toLowerCase().indexOf(ql) >= 0; }).slice(0, 40);
    var lastG = null;
    all.forEach(function (item, i) {
      if (item.g !== lastG) { lastG = item.g; list.appendChild(h("li", { class: "cl-palette__group", role: "presentation" }, item.g)); }
      var li = h("li", { id: "cl-pal-opt-" + i, role: "option", "aria-selected": String(i === 0), onclick: function () { closePalette(); go(item.hash); } }, item.label);
      list.appendChild(li); palItems.push(li);
    });
    // Point the combobox input at the active option so SR users hear the highlighted row.
    var inp = $("#cl-palette-input");
    if (palItems.length) inp.setAttribute("aria-activedescendant", palItems[0].id);
    else inp.removeAttribute("aria-activedescendant");
  }
  function palMove(d) { if (!palItems.length) return; palItems[palSel].setAttribute("aria-selected", "false"); palSel = (palSel + d + palItems.length) % palItems.length; var el = palItems[palSel]; el.setAttribute("aria-selected", "true"); el.scrollIntoView({ block: "nearest" }); $("#cl-palette-input").setAttribute("aria-activedescendant", el.id); }

  // ---- theme / density / rail ------------------------------------------
  function setTheme(t) {
    document.documentElement.dataset.theme = t; lsSet("theme", t);
    var f = $("#cl-iframe"); if (f && f.contentWindow) { try { f.contentWindow.postMessage({ type: "cl-theme", theme: t }, "*"); } catch (e) { } }
  }
  function initChrome() {
    // theme: stored, else prefers-color-scheme (floor: graph honors the same at load)
    var stored = lsGet("theme", null);
    setTheme(stored || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
    $("#cl-theme").onclick = function () { setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"); };
    // density
    var dens = lsGet("density", "comfortable"); document.documentElement.dataset.density = dens;
    // The visible text flips between "Compact" and "Comfortable" while the old
    // aria-label stayed frozen at "Toggle density", so a speech-input user reading
    // the button and saying "click Compact" matched nothing (WCAG 2.5.3). The name
    // is rebuilt from the visible word every time the word changes.
    function paintDensity(el, d) {
      var word = d === "compact" ? "Comfortable" : "Compact";
      el.textContent = word;
      el.setAttribute("aria-label", word + " density");
    }
    paintDensity($("#cl-density"), dens);
    $("#cl-density").onclick = function () {
      var d = document.documentElement.dataset.density === "compact" ? "comfortable" : "compact";
      document.documentElement.dataset.density = d; lsSet("density", d);
      paintDensity(this, d);
    };
    // info popover ("What am I looking at?")
    var infoBtn = $("#cl-info"), infoPop = $("#cl-infopop");
    // Opening it must MOVE focus into the panel. The panel sits after </header> in
    // the DOM, so without this the next three Tab stops after opening were the
    // density toggle, the theme toggle, and only then the panel's own Close button
    // -- two controls that change the whole UI, sitting between the user and the
    // thing that just appeared (WCAG 2.4.3). Closing already restores focus to the
    // button that opened it.
    function setInfo(open) {
      infoPop.hidden = !open;
      infoBtn.setAttribute("aria-expanded", String(open));
      if (open) $("#cl-info-close").focus();
    }
    infoBtn.onclick = function (e) { e.stopPropagation(); setInfo(infoPop.hidden); };
    $("#cl-info-close").onclick = function () { setInfo(false); infoBtn.focus(); };
    document.addEventListener("click", function (e) {
      if (!infoPop.hidden && !infoPop.contains(e.target) && !infoBtn.contains(e.target)) setInfo(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !infoPop.hidden) { setInfo(false); infoBtn.focus(); }
    });
    // rail collapse
    if (lsGet("rail", "open") === "collapsed") document.documentElement.dataset.rail = "collapsed";
    $("#cl-railtoggle").onclick = function () { var c = document.documentElement.dataset.rail === "collapsed"; document.documentElement.dataset.rail = c ? "open" : "collapsed"; lsSet("rail", c ? "open" : "collapsed"); };
    // mode badge
    var mb = $("#cl-mode");
    if (MODE === "static") { mb.className = "cl-mode cl-mode--static"; mb.textContent = "Static · " + (SNAP.snapshot_date || "snapshot"); }
    else mb.textContent = "Live";
    // ground-truth filter buttons
    document.querySelectorAll(".cl-gt").forEach(function (b) { b.onclick = function () { gt[b.dataset.conf] = !gtActive(b.dataset.conf); syncGT(); CL.router.render(); }; });
    // skip links must MOVE FOCUS, not route — their href is an in-page id, and letting
    // it hit location.hash would fire the hash router (lens "app"/"arch-tables" -> fleet).
    document.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest(".cl-skip");
      if (!a) return;
      e.preventDefault();
      var t = document.getElementById(a.getAttribute("href").slice(1));
      if (t) { t.setAttribute("tabindex", "-1"); t.focus(); }
    });
    // cmd-k + drawer close
    $("#cl-cmdk").onclick = openPalette;
    $("#cl-drawer-close").onclick = closeDrawer;
    $("#cl-palette-wrap").addEventListener("click", function (e) { if (e.target === this) closePalette(); });
    $("#cl-palette-input").addEventListener("input", function () { paintPalette(this.value); });
    $("#cl-palette-input").addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); palMove(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); palMove(-1); }
      else if (e.key === "Enter") { if (palItems[palSel]) palItems[palSel].click(); }
      else if (e.key === "Escape") closePalette();
      // Trap Tab inside the modal palette: the input is the only focusable control, so
      // swallowing Tab keeps focus from escaping behind the open dialog (WCAG 2.4.3).
      else if (e.key === "Tab") { e.preventDefault(); }
    });
    // Single-key shortcut opt-out (WCAG 2.1.4). `/` and `p` fired from anywhere on
    // the page with no way to turn them off, so a speech-input user dictating, or
    // anyone with a tremor or a stuck key, got yanked to the search lens or had a
    // provenance drawer thrown open (and focus moved into it) unasked. The old
    // guard also only checked `input, textarea`, so typeahead in a <select> or
    // inside contenteditable still triggered them.
    var scToggle = $("#cl-shortcuts-toggle");
    function shortcutsOn() { return lsGet("shortcuts", "on") !== "off"; }
    if (scToggle) {
      scToggle.checked = shortcutsOn();
      scToggle.addEventListener("change", function () {
        lsSet("shortcuts", this.checked ? "on" : "off");
        live(this.checked ? "Single-key shortcuts on" : "Single-key shortcuts off");
      });
    }
    function inTextEntry(t) {
      if (!t || !t.closest) return false;
      return !!t.closest("input, textarea, select, [contenteditable='']," +
        " [contenteditable='true'], [contenteditable='plaintext-only']");
    }
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); return; }
      // Escape is exempt from 2.1.4 (it is not a printable character key) and is
      // the only way out of the palette and the drawer, so it stays unconditional.
      if (e.key === "Escape") { closePalette(); closeDrawer(); return; }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (inTextEntry(e.target)) return;
      if (!shortcutsOn()) return;
      if (e.key === "/") { e.preventDefault(); go("#/search"); setTimeout(function () { var f = $("#cl-searchfield"); if (f) f.focus(); }, 30); }
      else if (e.key === "P" || e.key === "p") { openDrawer(null); }
    });
  }

  // ---- boot -------------------------------------------------------------
  function boot() {
    initChrome();
    window.addEventListener("hashchange", function () { CL.router.render(); });
    if (!location.hash) location.hash = "#/fleet";
    CL.router.render();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.CL = CL;
})();
