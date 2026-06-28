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
    INFERRED: ["Inferred", "#E7B53C", "Deduced — second-pass / heuristic"],
    AMBIGUOUS: ["Ambiguous", "#e76f51", "Uncertain — flagged for review"]
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
    function: 1, method: 1, package: 1, repo: 1, issue: 1, design: 1, endpoint: 1, topic: 1
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
    parent.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
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
    health: function () {
      return MODE === "static" ? Promise.resolve(SNAP.health) : fetchJSON("/api/health");
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
    symbols: function () { return MODE === "static" ? (SNAP.symbols || []) : null; }
  };

  // ---- context spine ----------------------------------------------------
  var ctx = { domain: null, repoId: null, nodeId: null };

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
  var OTTER = '<svg class="cl-state__otter" aria-hidden="true" viewBox="0 0 24 24"><use href="#ui-otter"></use></svg>';
  function stateBlock(opts) {
    var mod = opts.kind ? " cl-state--" + opts.kind : "";
    var box = h("div", { class: "cl-state" + mod, role: "status" });
    if (opts.kind === "empty" || opts.kind === "ok") box.appendChild(h("span", { html: OTTER }));
    box.appendChild(h("p", { class: "cl-state__title" }, opts.title || ""));
    if (opts.msg) box.appendChild(h("p", null, opts.msg));
    if (opts.cmd) box.appendChild(h("code", null, opts.cmd));
    if (opts.action) box.appendChild(opts.action);
    return box;
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
      renderInto(bodyId, stateBlock({
        kind: "error", title: "Couldn't load this view",
        msg: MODE === "live" ? "The data source didn't respond (" + e.message + ")." : "Missing from this snapshot.",
        action: h("button", { class: "cl-btn", type: "button", onclick: function () { CL.router.render(); } }, "Retry")
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
      keys.appendChild(h("span", null, confChip(c), " ", h("strong", null, String(n)), " · " + pct + "%"));
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
          h("div", { class: "cl-stat__num" }, String(s[p[0]] != null ? s[p[0]] : "—")),
          h("div", { class: "cl-stat__cap" }, p[1])));
      });
      body.appendChild(stats);
      body.appendChild(h("div", { class: "cl-card" }, trustBar(s.by_confidence || {}, { label: "Knowledge confidence" })));

      if (!ov.repos || !ov.repos.length) {
        body.appendChild(stateBlock({
          kind: "empty", title: "No repos indexed yet",
          msg: "Index a workspace to fill the lake.", cmd: "contextlake index"
        }));
        return body;
      }

      var byGroup = {};
      ov.repos.forEach(function (r) { (byGroup[r.group] = byGroup[r.group] || []).push(r); });
      var groups = Object.keys(byGroup).sort();
      var openState = JSON.parse(lsGet("bands", "{}") || "{}");
      groups.forEach(function (g, gi) {
        var repos = byGroup[g];
        var isOpen = openState[g] != null ? openState[g] : (gi === 0);
        var grid = h("div", { class: "cl-grid", role: "list" });
        repos.forEach(function (r) { grid.appendChild(repoCard(r)); });
        var det = h("details", { class: "cl-band" });
        if (isOpen) det.open = true;
        det.appendChild(h("summary", null,
          h("span", { class: "cl-band__name" }, g),
          h("span", { class: "cl-band__count" }, repos.length + " repos")));
        det.appendChild(grid);
        det.addEventListener("toggle", function () {
          openState[g] = det.open; lsSet("bands", JSON.stringify(openState));
        });
        body.appendChild(det);
      });
      return body;
    });
  }
  function repoCard(r) {
    var health = r.indexed_at ? "fresh" : "stale";
    var card = h("button", {
      type: "button", class: "cl-repocard", role: "listitem",
      onclick: function () { go("#/repo/" + r.id); }
    },
      h("div", { class: "cl-repocard__top" },
        kindIcon("repo"),
        h("span", { class: "cl-repocard__name" }, r.id),
        lettermarks(r.langs),
        h("span", { class: "cl-healthchip cl-healthchip--" + health, title: health })),
      h("div", { class: "cl-repocard__hidden" },
        (r.node_count || 0) + " nodes · " + (r.default_branch || "—") +
        (r.head_commit ? " · " + String(r.head_commit).slice(0, 8) : "")));
    return card;
  }

  // ---- Repo detail ------------------------------------------------------
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
          h("button", { class: "cl-btn cl-btn--primary", type: "button", onclick: function () { go("#/arch/" + id); } },
            h("span", { html: icon("ui-arch") }), "View in architecture"))));

      var tabs = ["anatomy", "readme", "wiki", "owners", "links"];
      var cur = tabs.indexOf(tab) >= 0 ? tab : "anatomy";
      var strip = h("div", { class: "cl-tabs", role: "tablist" });
      var pane = h("div", { class: "cl-panel__body" });
      tabs.forEach(function (t) {
        strip.appendChild(h("button", {
          class: "cl-tab", role: "tab", type: "button", "aria-selected": String(t === cur),
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
    } else if (tab === "readme") {
      if (d.readme_html) pane.appendChild(h("div", { class: "cl-card" },
        h("p", { class: "cl-muted" }, "README — verbatim from source" + (b.head ? " @ " + String(b.head).slice(0, 8) : "")),
        h("div", { class: "cl-md", html: d.readme_html })));
      else pane.appendChild(stateBlock({ kind: "empty", title: "No README found in this repo" }));
    } else if (tab === "wiki") {
      var w = d.wiki || {};
      if (!w.found) { pane.appendChild(stateBlock({ kind: "empty", title: "No wiki generated for this repo", cmd: "contextlake wiki" })); return; }
      var revealed = false;
      var holder = h("div", { class: "cl-advisory" },
        h("strong", null, "Curated wiki — advisory" + (w.stale ? ", may be stale" : "")),
        h("p", { class: "cl-muted" }, "Not ground truth. Reveal to read; verify against the cited source."));
      var btn = h("button", { class: "cl-btn", type: "button" }, "Reveal wiki");
      btn.addEventListener("click", function () {
        if (revealed) return; revealed = true; btn.remove();
        holder.appendChild(h("div", { class: "cl-md", html: w.html || "" }));
      });
      holder.appendChild(btn); pane.appendChild(holder);
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
      if (!keys.length) { pane.appendChild(stateBlock({ kind: "empty", title: "No connector links found", msg: "No Jira / Confluence / Figma / GitLab cross-links." })); return; }
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
    }
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
    if (!target) {
      tablesWrap.appendChild(stateBlock({ kind: "empty", title: "Pick a repo to see its relationship tables", msg: "The accessible equal of the graph above.", action: h("button", { class: "cl-btn cl-btn--primary", type: "button", onclick: function () { go("#/fleet"); } }, "Open fleet") }));
      return;
    }
    tablesWrap.appendChild(skeleton(2));
    CL.data.rel(target).then(function (rel) {
      clear(tablesWrap);
      var sub = ["dependencies", "http_flow", "event_flow"];
      var names = { dependencies: "Dependencies", http_flow: "HTTP flow", event_flow: "Event flow" };
      var cur = "dependencies";
      var strip = h("div", { class: "cl-tabs", role: "tablist" });
      var pane = h("div", null);
      function paint(k) {
        clear(pane);
        var rows = (rel[k] || []).filter(function (e) { return gtActive(e.confidence); });
        if (!rows.length) { pane.appendChild(stateBlock({ kind: "empty", title: "No " + names[k].toLowerCase() + " for this scope" })); return; }
        var maxW = rows.reduce(function (a, e) { return Math.max(a, e.weight || 1); }, 1);
        pane.appendChild(table(["Source", "Target", "Relation", "Confidence", "Weight", ""],
          rows.map(function (e) {
            return [e.src, e.dst, e.relation, confChip(e.confidence),
              h("span", { class: "cl-flowbar", style: "width:" + Math.max(6, Math.round((e.weight || 1) / maxW * 60)) + "px" }),
              citeButton({ claim: e.src + " → " + e.dst, repo: e.src, source: e.context || "manifest/regex", confidence: e.confidence, note: e.confidence === "AMBIGUOUS" ? "Flagged uncertain — verify." : "" })];
          }), [false, false, false, false, true, false],
          rows.map(function (e) { return e.confidence === "AMBIGUOUS"; })));
      }
      sub.forEach(function (k) {
        strip.appendChild(h("button", {
          class: "cl-tab", role: "tab", type: "button", "aria-selected": String(k === cur),
          onclick: function () { strip.querySelectorAll(".cl-tab").forEach(function (t) { t.setAttribute("aria-selected", "false"); }); this.setAttribute("aria-selected", "true"); paint(k); }
        }, names[k] + " (" + (rel[k] || []).length + ")"));
      });
      tablesWrap.appendChild(strip); tablesWrap.appendChild(pane); paint(cur);
    }).catch(function () {
      clear(tablesWrap);
      tablesWrap.appendChild(stateBlock({ kind: "error", title: "Couldn't load relationships" }));
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
    ctx.nodeId = seed; refreshChrome();
    // Always load the payload at the widest hops (3) so the slider/relation/cross
    // controls are pure client-side filters: narrowing re-paints in place (no re-fetch,
    // no skeleton flash, no rebuilt slider). Static already returns the hops=3
    // precompute; in live mode requesting max hops is what makes client narrowing correct.
    asyncPanel("symbol-body", function () { return CL.data.impact(seed, 3, blastCfg.limit); }, function (imp) {
      var body = h("div", { class: "cl-panel__body" });
      if (!imp.found) {
        return stateBlock({
          kind: imp.static_missing ? "unavailable" : "empty",
          title: imp.static_missing ? "Not precomputed in this snapshot" : "Symbol not found",
          msg: imp.static_missing ? "This export ships a representative slice. Run the live server to trace any symbol." : "No node matched \"" + seed + "\"."
        });
      }
      body.appendChild(h("div", { class: "cl-card cl-sectionhead" },
        h("div", { class: "cl-row" }, kindIcon("function"), h("strong", null, imp.name || seed),
          citeButton({ claim: imp.name || seed, repo: (imp.seed || "").split(":")[0], source: imp.seed, confidence: "EXTRACTED" }))));

      // The impact payload carries no seed repo; resolve it from the symbol index so
      // "cross-repo only" is real, not a silent no-op. When unresolvable (live mode, no
      // local index) the control is disabled rather than dead.
      var seedSym = (CL.data.symbols() || []).filter(function (s) { return s.id === imp.seed; })[0];
      var seedRepo = seedSym ? seedSym.repo : null;
      var crossKnown = seedRepo != null;
      if (!crossKnown) blastCfg.crossOnly = false;

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
          var lane = h("div", { class: "cl-lane", role: "list", "aria-label": "Hop " + hop });
          lane.appendChild(h("div", { class: "cl-lane__head" }, "Hop " + hop));
          hits.filter(function (hi) { return hi.hop === hop; }).forEach(function (hi) {
            lane.appendChild(h("button", {
              type: "button", role: "listitem", class: "cl-hit cl-hit--" + String(hi.confidence).toLowerCase(),
              onclick: function () { go("#/symbol/" + hi.id); }
            }, kindIcon(hi.kind),
              h("span", { class: "cl-hit__name" }, hi.name),
              h("span", { class: "cl-hit__via" }, hi.repo + " · via " + hi.via)));
          });
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
      return body;
    });
  }
  function toggleBtn(rel, label, onChange) {
    return h("button", { class: "cl-btn", type: "button", "aria-pressed": String(blastCfg.rels[rel] !== false), onclick: function () { blastCfg.rels[rel] = blastCfg.rels[rel] === false; this.setAttribute("aria-pressed", String(blastCfg.rels[rel] !== false)); if (onChange) onChange(); else CL.router.render(); } }, label);
  }
  function labelWrap(text, ctrl) { return h("label", { class: "cl-row" }, text, ctrl); }

  // ---- Health -----------------------------------------------------------
  function viewHealth() {
    asyncPanel("health-body", CL.data.health, function (hd) {
      var body = h("div", { class: "cl-panel__body" });
      var clean = !hd.stale && !hd.dangling;
      body.appendChild(h("div", { class: "cl-statgrid" },
        statTile(hd.checked, "Checked"), statTile(hd.stale, "Stale repos"), statTile(hd.dangling, "Dangling edges")));
      if (clean) { body.appendChild(stateBlock({ kind: "ok", title: "Clear water", msg: "No stale repos, no dangling edges." })); return body; }
      if (hd.stale_repos && hd.stale_repos.length) {
        var sc = h("div", { class: "cl-card" }, h("strong", null, "Stale repos"));
        hd.stale_repos.forEach(function (r) {
          sc.appendChild(h("div", { class: "cl-row" },
            h("button", { class: "cl-btn", type: "button", onclick: function () { go("#/repo/" + r); } }, r),
            h("span", { class: "cl-healthchip cl-healthchip--stale" }, "HEAD moved"),
            h("code", null, "contextlake index")));
        });
        body.appendChild(sc);
      }
      if (hd.dangling_sample && hd.dangling_sample.length) {
        body.appendChild(table(["Repo", "Source", "Relation", "Missing target"],
          hd.dangling_sample.map(function (d) { return [d.repo, d.src, d.relation, d.dst]; })));
      }
      return body;
    });
  }
  function statTile(n, cap) { return h("div", { class: "cl-stat" }, h("div", { class: "cl-stat__num" }, String(n != null ? n : "—")), h("div", { class: "cl-stat__cap" }, cap)); }

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
        results.appendChild(stateBlock({ kind: "unavailable", title: "Semantic search is live-only", msg: "Needs the running server.", cmd: "contextlake serve" }));
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
          results.appendChild(h("button", {
            type: "button", class: "cl-result", onclick: function () { go("#/repo/" + n.repo + "?tab=anatomy"); }
          }, kindIcon(n.kind),
            h("span", null, h("strong", null, n.qualified_name || n.name),
              h("div", { class: "cl-result__meta" }, n.repo + (n.file ? " · " + n.file + (n.line ? ":" + n.line : "") : ""))),
            h("button", { type: "button", class: "cl-btn", onclick: function (ev) { ev.stopPropagation(); go("#/symbol/" + (n.id || n.name)); } }, "Blast")));
        });
      }).catch(function (e) { clear(results); results.appendChild(stateBlock({ kind: "error", title: "Search failed", msg: String(e.message || e) })); });
    }
    field.addEventListener("input", debounce(runSearch, 200));
    field.focus();
    if (searchState.q) runSearch();
  }

  // ---- generic table (with pagination) ----------------------------------
  function num(n) { return n == null ? "—" : String(n); }
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
  var PANELS = ["fleet", "repo", "arch", "symbol", "health", "search"];
  // Track the last-rendered route so we only move focus to #app on an actual
  // route/lens CHANGE (navigation), never on in-view data re-renders — e.g. the
  // ground-truth filter, trust-bar segments and blast toggles all call
  // CL.router.render() with an unchanged hash, and stealing focus there breaks
  // WCAG 2.4.3 (focus order). Tab switches change the hash, so they do refocus.
  var lastRouteSig = null;
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
      if (sig !== lastRouteSig) { lastRouteSig = sig; $("#app").focus({ preventScroll: false }); }
      if (lens === "fleet") viewFleet();
      else if (lens === "repo") viewRepo(r.rest || ctx.repoId, r.query.tab);
      else if (lens === "arch") viewArch(r.rest || null);
      else if (lens === "symbol") viewSymbol(r.rest || ctx.nodeId);
      else if (lens === "health") viewHealth();
      else if (lens === "search") { if (r.query.q) searchState.q = r.query.q; viewSearch(searchState.q); }
      refreshChrome(lens);
    }
  };

  function refreshChrome(lens) {
    var ol = clear($("#cl-crumbs"));
    function crumb(label, hash, current) {
      var li = h("li");
      li.appendChild(h("button", { class: "cl-crumb", type: "button", "aria-current": current ? "page" : null, onclick: function () { if (hash) go(hash); } }, label));
      ol.appendChild(li);
    }
    crumb("Lake", "#/fleet", lens === "fleet");
    if (ctx.repoId) {
      if (ctx.repoId.indexOf("/") >= 0) crumb(ctx.repoId.split("/")[0], "#/fleet");
      crumb(ctx.repoId, "#/repo/" + ctx.repoId, lens === "repo");
    }
    if (ctx.nodeId && lens === "symbol") crumb(String(ctx.nodeId).split("/").pop(), null, true);
    // pinned chip
    var pin = $("#cl-pinchip");
    if (ctx.repoId) {
      pin.hidden = false; clear(pin);
      pin.appendChild(h("span", { html: icon("ui-pin") }));
      pin.appendChild(document.createTextNode(ctx.repoId));
      pin.onclick = function () { ctx.repoId = null; ctx.nodeId = null; refreshChrome(); live("Context cleared"); };
      pin.setAttribute("aria-label", "Clear pinned " + ctx.repoId);
    } else pin.hidden = true;
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
    $("#cl-density").textContent = dens === "compact" ? "Comfortable" : "Compact";
    $("#cl-density").onclick = function () { var d = document.documentElement.dataset.density === "compact" ? "comfortable" : "compact"; document.documentElement.dataset.density = d; lsSet("density", d); this.textContent = d === "compact" ? "Comfortable" : "Compact"; };
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
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); return; }
      if (e.target.matches("input, textarea")) return;
      if (e.key === "/") { e.preventDefault(); go("#/search"); setTimeout(function () { var f = $("#cl-searchfield"); if (f) f.focus(); }, 30); }
      else if (e.key === "P" || e.key === "p") { openDrawer(null); }
      else if (e.key === "Escape") { closePalette(); closeDrawer(); }
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
