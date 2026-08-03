function edgeColor(e){ return REL_COLORS[e.data("relation")] || DEFAULT_EDGE_COLOR; }
  // Only the *architectural* relations get an edge label — labelling the hundreds of
  // structural calls/contains/imports edges would bury the diagram in text.
  var ARCH_RELS = { depends_on: 1, flow: 1, calls_http: 1, exposes: 1,
                    publishes: 1, publishes_event: 1, consumes_event: 1 };
  // contexts that are internal markers, not a human-meaningful path/package/topic
  var GENERIC_CTX = { "": 1, ambiguous: 1, event: 1, http: 1 };
  function edgeLabel(e){
    var r = e.data("relation");
    if (!ARCH_RELS[r]) return "";
    var ctx = e.data("context");
    return (ctx && !GENERIC_CTX[ctx]) ? r + " · " + ctx : r;
  }
  function cssVar(n){ return getComputedStyle(document.body).getPropertyValue(n).trim(); }
  var RM = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  function dur(ms){ return RM.matches ? 0 : ms; }   // collapse motion to instant under reduced-motion

  // The cytoscape stylesheet is rebuilt on theme change: CSS variables can't reach
  // canvas pixels, so node-label / highlight text colours are re-read here. (node.hi
  // is a RING, not a background swap, so it reads on both light and dark themes.)
  function graphStyle(){
    var label = cssVar("--canvas-label") || "#0E2A33";
    var surf = cssVar("--surface-solid") || "#ffffff";
    return [
      { selector: "node", style: {
          "background-color": function(n){ return COLORS[n.data("kind")] || DEFAULT_COLOR; },
          // type glyph painted onto the node (data-URI, offline) — reads by kind at a
          // glance; repo nodes show their primary-language lettermark (tech stack) instead
          "background-image": function(n){
            if (n.data("kind") === "repo" && LANG_ICONS[n.data("lang")]) return LANG_ICONS[n.data("lang")];
            return ICONS[n.data("kind")] || "none";
          },
          "background-fit": "none", "background-clip": "none",
          "background-width": "58%", "background-height": "58%",
          "background-image-opacity": 0.96,
          "label": "data(label)", "font-size": 9, "color": label,
          "width": "mapData(deg, 0, 24, 20, 56)", "height": "mapData(deg, 0, 24, 20, 56)",
          "text-wrap": "ellipsis", "text-max-width": 120,
          "text-valign": "bottom", "text-margin-y": 2,
          "border-width": 0.5, "border-color": surf } },
      { selector: "edge", style: {
          "line-color": edgeColor, "target-arrow-color": edgeColor,
          "width": "mapData(weight, 1, 10, 0.8, 4.5)",
          "target-arrow-shape": "triangle", "arrow-scale": 0.7, "curve-style": "bezier",
          // labelled flows: relation (+ path/package/topic) on architectural edges only
          "label": edgeLabel, "font-size": 7, "color": label,
          "text-rotation": "autorotate", "text-margin-y": -3,
          "text-background-color": surf, "text-background-opacity": 0.85,
          "text-background-padding": 2, "text-background-shape": "roundrectangle" } },
      { selector: 'edge[confidence = "EXTRACTED"]',
        style: { "line-style": "solid", "opacity": 0.7 } },
      { selector: 'edge[confidence = "INFERRED"]',
        style: { "line-style": "dashed", "opacity": 0.55 } },
      { selector: 'edge[confidence = "AMBIGUOUS"]',
        style: { "line-style": "dotted", "opacity": 0.45 } },
      { selector: ".faded", style: {
          "opacity": (parseFloat(cssVar("--faded-opacity")) || 0.1), "text-opacity": 0 } },
      // level-of-detail labels: at low zoom only high-degree hubs keep their text
      // (driven by applyLOD). dim-label hides; lbl-on (hover) and hi/found (highlight,
      // search) force it back on. lbl-on sits AFTER dim-label so it wins on a tie.
      { selector: "node.dim-label", style: { "text-opacity": 0 } },
      { selector: "node.lbl-on", style: { "text-opacity": 1 } },
      { selector: "node.hi", style: { "border-width": 3, "border-color": "#2BB3A3",
          "text-opacity": 1, "z-index": 99 } },
      { selector: "node.found", style: { "border-width": 4, "border-color": "#E7B53C",
          "text-opacity": 1, "z-index": 100 } },
      { selector: "edge.hi", style: { "width": 2.2, "opacity": 1,
          "label": "data(relation)", "font-size": 7, "color": label,
          "text-rotation": "autorotate", "text-background-color": surf,
          "text-background-opacity": 0.9, "z-index": 99 } },
      // overview namespace mindmap: cluster nodes, faint "contains" spokes, and
      // aggregated namespace-to-namespace dependency edges
      { selector: 'node[kind = "namespace"]', style: {
          "shape": "round-rectangle", "background-color": "#137A8B",
          "background-opacity": 0.13, "border-width": 1.5, "border-color": "#137A8B",
          "label": "data(label)", "font-size": 12, "font-weight": 600, "color": label,
          "text-valign": "center", "text-halign": "center", "text-wrap": "wrap",
          "text-max-width": 130, "text-margin-y": 0,
          "width": "mapData(count, 1, 120, 46, 130)",
          "height": "mapData(count, 1, 120, 46, 130)", "z-index": 2 } },
      { selector: 'edge[scaffold]', style: {
          "line-color": "#9bbcc2", "width": 0.7, "target-arrow-shape": "none",
          "opacity": 0.4, "curve-style": "straight" } },
      // "dagre (preview)" only: the canvas node is blanked so the real HTML card
      // (cytoscape-dom-node) is all you see. Never applied under any other layout.
      { selector: "node.cl-dom", style: {
          "background-opacity": 0, "background-image": "none",
          "border-width": 0, "label": "" } },
      { selector: 'edge[aggregated]', style: {
          "width": "mapData(weight, 1, 20, 1.6, 7)", "opacity": 0.8,
          "label": "data(weight)", "font-size": 10, "font-weight": 600, "color": label,
          "text-background-color": surf, "text-background-opacity": 0.9,
          "text-background-padding": 2, "text-rotation": "autorotate" } }
    ];
  }

  var cyEl = document.getElementById("cy");
  var cy = cytoscape({
    container: cyEl,
    elements: ELEMENTS,
    wheelSensitivity: 0.2,
    // Clamp wheel-zoom so the graph can't shrink to unreadable specks or balloon
    // past usefulness; fitClamped (below) enforces a higher readable floor on "fit".
    minZoom: 0.06, maxZoom: 2.5,
    style: graphStyle(),
    layout: { name: "preset" }
  });

  // Keep the cytoscape <canvas> synced to its grid cell through ANY layout change
  // (inspector slide-in, sidebar collapse, window resize) — robust, no timing
  // guess. cy.resize() re-reads the container each frame the cell animates.
  // ALSO re-FIT once the container first gets real size: when embedded in an iframe
  // (or a panel that's hidden until routed to), cytoscape lays out against a 0-size
  // viewport and paints nodes off-screen — they only appear after a manual zoom/
  // resize forces a repaint. A bare cy.resize() keeps the bad pan/zoom (still
  // clipped), so the first real-size tick must reframe, exactly once.
  var initialFramed = false;
  function frameInitial(){
    if(initialFramed) return;
    if(cyEl.clientWidth > 0 && cyEl.clientHeight > 0){
      initialFramed = true; cy.resize(); reframe(); applyLOD(true);
    }
  }
  if(window.ResizeObserver){
    new ResizeObserver(function(){ cy.resize(); frameInitial(); }).observe(cyEl);
  }

  // deg is set server-side (visualize.py:_cytoscape_elements) so it's present from
  // the very first style pass -- recomputing it here on the untouched initial graph
  // would just reproduce the same numbers cytoscape.js's own degree() would give.
  document.getElementById("mode").textContent = META.mode || "graph";
  // In the fleet overview, repos with no detected cross-repo dependency are hidden
  // by default (they dominate and convey no structure) \u2014 kept in the graph and
  // findable via search. "no detected dependency" is honest: the two-hop resolver
  // is a known undercount, so absence here is not proof a repo is truly isolated.
  var OVERVIEW = (META.mode === "overview");
  function isNoDep(n){ return OVERVIEW && n.data("deg") === 0; }
  var noDepCount = cy.nodes().filter(isNoDep).length;
  document.getElementById("meta").textContent =
    cy.nodes().length + " nodes \u00b7 " + cy.edges().length + " edges"
    + (noDepCount ? " \u00b7 " + noDepCount + " with no detected dependency" : "");
  if(!cy.nodes().length){ document.getElementById("empty").classList.add("show"); }
  // honesty: when the view was capped, say so (never imply completeness)
  if(META.truncated){
    var tb = document.getElementById("trunc");
    tb.textContent = "\u26a0 showing " + cy.nodes().length
      + (META.total ? " of " + META.total : "") + " \u2014 truncated; raise --max-nodes";
    tb.classList.add("show");
  }

  // theme toggle — re-skins the canvas (CSS vars don't reach canvas pixels)
  document.getElementById("theme").onclick = function(){
    document.body.dataset.theme = document.body.dataset.theme === "dark" ? "light" : "dark";
    cy.style(graphStyle());
  };
  if(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches){
    document.body.dataset.theme = "dark"; cy.style(graphStyle());
  }
  // Optional dashboard coordination (file://-safe, null-origin tolerant): honor an
  // explicit ?theme=/#theme= on the initial src, and a postMessage from an embedding
  // dashboard — both just ride the existing theme path (dataset + graphStyle rebuild).
  (function(){
    function applyTheme(t){
      if(t !== "dark" && t !== "light") return;
      document.body.dataset.theme = t; cy.style(graphStyle());
    }
    var m = /[?#&]theme=(dark|light)/.exec(location.href);
    if(m){ applyTheme(m[1]); }
    window.addEventListener("message", function(e){
      var d = e && e.data;
      if(d && d.type === "cl-theme"){ applyTheme(d.theme); }
    });
  })();
  document.getElementById("navToggle").onclick = function(){
    var c = document.body.dataset.sidebar === "collapsed";
    document.body.dataset.sidebar = c ? "open" : "collapsed"; afterResize();
  };
  // Fit, but never below a readable floor: cy.fit on a large graph drives the zoom
  // so low that nodes become illegible specks (the #1 complaint). Fit, then if we
  // landed under the floor, snap up to it and re-center on the framed elements
  // (cy.fit sets pan AFTER zoom, so the recentre must come last). 0.45 lands in the
  // LOD tier where hub labels are still drawn, so a fit is always at least scannable.
  var FIT_FLOOR = 0.45;
  function fitClamped(eles, padding){
    cy.fit(eles, padding);
    if(cy.zoom() < FIT_FLOOR){ cy.zoom(FIT_FLOOR); cy.center(eles); }
  }
  function fitClampedAnimated(eles, padding, ms){
    if(!dur(ms)){ fitClamped(eles, padding); return; }   // reduced motion -> instant
    cy.animate({ fit: { eles: eles, padding: padding } }, { duration: dur(ms),
      complete: function(){
        if(cy.zoom() < FIT_FLOOR){ cy.zoom(FIT_FLOOR); cy.center(eles); }
      } });
  }
  // "Fit" frames the readable view: the connected core when isolated repos
  // dominate (the fleet overview), else the whole graph.
  function reframe(){
    var core = cy.nodes().filter(function(n){ return n.degree(false) > 0; });
    var dominated = core.nonempty() && (cy.nodes().length - core.length) > core.length;
    fitClamped(dominated ? core : undefined, 30);
  }
  document.addEventListener("keydown", function(e){
    if(e.target.tagName === "INPUT"){ if(e.key === "Escape"){ e.target.blur(); } return; }
    if(e.key === "/"){ e.preventDefault(); document.getElementById("search").focus(); }
    else if(e.key === "f" || e.key === "F"){ reframe(); }
    else if(e.key === "t" || e.key === "T"){ document.getElementById("theme").click(); }
    else if(e.key === "Escape"){
      cy.elements().removeClass("faded hi"); hideInfo(); refreshDomFx(); stopAnts();
    }
  });

  function layoutOpts(name){
    if(name === "cose") return { name:"cose", animate:false, randomize:true, padding:40,
        nodeOverlap:24, componentSpacing:140, gravity:0.2, numIter:1500,
        nodeRepulsion:function(){ return 14000; },
        idealEdgeLength:function(){ return 120; }, edgeElasticity:function(){ return 80; } };
    if(name === "concentric") return { name:"concentric", animate:false, padding:40,
        minNodeSpacing:28, nodeDimensionsIncludeLabels:true,
        concentric:function(n){ return n.degree(false); },
        levelWidth:function(){ return 2; } };
    if(name === "breadthfirst") return { name:"breadthfirst", animate:false, padding:40,
        spacingFactor:1.5, circle:false };
    if(name === "circle") return { name:"circle", animate:false, padding:40, spacingFactor:1.3 };
    if(name === "grid") return { name:"grid", animate:false, padding:40, avoidOverlap:true,
        avoidOverlapPadding:24 };
    // dagre: layered/hierarchical — the one layout that reads dependency DIRECTION as
    // top-to-bottom ranks instead of a force-directed blob. rankSep is generous because
    // the preview's HTML cards are much wider than the canvas circles they replace.
    if(name === DAGRE) return { name:DAGRE, animate:false, padding:40, fit:false,
        rankDir:"TB", ranker:"network-simplex", nodeSep:34, edgeSep:12, rankSep:76,
        nodeDimensionsIncludeLabels:true };
    return { name:name, animate:false };
  }
  // Grid-pack a set of bounding-boxed groups (component tiles, or no-dep nodes) into
  // rows no wider than maxW, mutating each group's node positions into place.
  function packRows(groups, maxW, pad){
    var x = 0, y = 0, rowH = 0;
    groups.forEach(function(g){
      var bb = g.boundingBox();
      if(x > 0 && x + bb.w > maxW){ x = 0; y += rowH + pad; rowH = 0; }
      var dx = x - bb.x1, dy = y - bb.y1;
      g.nodes().positions(function(n){ var p = n.position(); return { x: p.x + dx, y: p.y + dy }; });
      x += bb.w + pad; rowH = Math.max(rowH, bb.h);
    });
  }
  // The fleet overview is dozens of small hub-and-satellite dependency clusters plus
  // many repos with no detected deps. A single global layout either scatters each
  // hub's dependents onto far rings (concentric) or collapses the disconnected
  // clusters into a sliver (cose). Instead, lay out EACH dependency cluster
  // compactly on its own (hub centred) and pack the cluster-tiles into a grid, so
  // the overview reads as a map of clusters. No-dep repos are parked (hidden) below.
  function runLayout(name){
    cy.layout(layoutOpts(name)).run();
    fitClamped(undefined, 30);
    // HTML cards have no level-of-detail fallback the way canvas labels do: below
    // ~0.7 the real typography that is the whole point of the mode stops being
    // readable. So in card mode we frame legibly and let the user pan, rather than
    // fitting the whole graph into unreadable confetti.
    if(domOn && cy.zoom() < CARD_FIT_FLOOR){ cy.zoom(CARD_FIT_FLOOR); cy.center(); }
    applyLOD(true);
  }

  // ===== "dagre (preview)": an OPT-IN alternative rendering, off by default. =====
  // Picking "dagre (preview)" in the layout dropdown does three things at once, and
  // picking any other layout undoes all three — the canvas rendering every other
  // layout uses is untouched by this block:
  //   1. lays the graph out in dagre's directed ranks (top-to-bottom flow),
  //   2. swaps the canvas circles for real HTML cards (cytoscape-dom-node), so nodes
  //      get border-radius, a shadow and real typography instead of painted glyphs,
  //   3. marches ants along the selected node's edges (cytoscape's own
  //      line-dash-offset animation — no extra library).
  // Both extensions are optional at RUNTIME: if their <script> didn't load (e.g. the
  // lazy `--serve` site does not yet serve them as siblings), the option removes
  // itself rather than offering a mode that would silently do nothing.
  var DAGRE = "dagre";
  var HAS_DAGRE = (typeof cytoscapeDagre !== "undefined");
  var HAS_DOMNODE = (typeof cytoscapeDomNode !== "undefined");
  if(HAS_DAGRE){ cytoscape.use(cytoscapeDagre); }
  (function(){
    var opt = document.querySelector('#layout option[value="' + DAGRE + '"]');
    if(opt && !HAS_DAGRE){ opt.remove(); }
  })();

  // An HTML card per node costs a DOM element, a ResizeObserver entry and a transform
  // per frame — worth it for a repo-sized graph, not for a fleet-sized one. Past the
  // cap the layout still switches to dagre; only the card rendering stays off, and
  // the status bar says so rather than leaving it looking broken.
  var DOM_NODE_CAP = 400, CARD_FIT_FLOOR = 0.7;
  var domRenderer = null, domBox = null, domOn = false;
  function noteMode(msg){
    var el = document.getElementById("rmode");
    if(el){ el.textContent = msg || ""; el.classList.toggle("show", !!msg); }
  }
  // The card mirrors what the canvas node encodes (kind colour, type/language glyph,
  // label) in real DOM. Text goes in via textContent — node labels are repo-derived
  // and must never be parsed as HTML.
  function domCard(n){
    var d = n.data(), kind = d.kind || "";
    var card = document.createElement("div");
    card.className = "cl-card";
    card.style.setProperty("--k", COLORS[kind] || DEFAULT_COLOR);
    var icon = (kind === "repo" && LANG_ICONS[d.lang]) ? LANG_ICONS[d.lang] : ICONS[kind];
    if(icon){
      var gi = document.createElement("span");
      gi.className = "ci";
      gi.style.backgroundImage = 'url("' + icon + '")';
      card.appendChild(gi);
    }
    var body = document.createElement("span");
    body.className = "cb";
    var t = document.createElement("span");
    t.className = "ct"; t.textContent = d.label || d.id;
    var k = document.createElement("span");
    k.className = "ck"; k.textContent = kind;
    body.appendChild(t); body.appendChild(k);
    card.appendChild(body);
    return card;
  }
  function enterDomMode(){
    if(domOn || !HAS_DOMNODE) return;
    var nodes = cy.nodes();
    if(nodes.length > DOM_NODE_CAP){
      noteMode("card view off — " + nodes.length + " nodes (cap " + DOM_NODE_CAP + ")");
      return;
    }
    // Own the layer rather than letting the extension create one: destroy() detaches
    // its handlers but leaves appended cards behind, so teardown has to be ours.
    var canvas = cyEl.querySelector("canvas");
    domBox = document.createElement("div");
    domBox.className = "cl-domlayer";
    (canvas && canvas.parentNode ? canvas.parentNode : cyEl).appendChild(domBox);
    cy.batch(function(){ nodes.forEach(function(n){ n.data("dom", domCard(n)); }); });
    // interactiveSelector:false — the cards are pure presentation (pointer-events:none
    // in CSS), so every gesture keeps falling through to cytoscape as it does today.
    domRenderer = cy.domNode({ domContainer: domBox, interactiveSelector: false });
    nodes.addClass("cl-dom");
    document.body.dataset.render = "cards";   // scopes #cy{overflow:hidden} to the preview
    domOn = true;
    syncDomVisibility(); refreshDomFx();
    noteMode("preview: HTML cards");
  }
  function exitDomMode(){
    if(!domOn) return;
    domOn = false;
    stopAnts();
    if(domRenderer && domRenderer.destroy){ domRenderer.destroy(); }
    domRenderer = null;
    cy.batch(function(){
      // remove ONLY the properties dom-node set inline (it writes width/height/shape);
      // a bare removeStyle() would also wipe the `display` the legend filters set.
      cy.nodes().removeClass("cl-dom").removeStyle("width height shape").removeData("dom");
    });
    if(domBox && domBox.parentNode){ domBox.parentNode.removeChild(domBox); }
    domBox = null;
    delete document.body.dataset.render;
    noteMode("");
  }
  function applyRenderMode(name){
    if(name === DAGRE && !OVERVIEW){ enterDomMode(); return; }
    exitDomMode();
    noteMode("");   // also clears the "cap" notice, where card mode never engaged
  }
  // dom-node syncs position/size/selection but NOT visibility, so a legend filter
  // would leave cards floating over the canvas for nodes it just hid.
  function syncDomVisibility(){
    if(!domOn) return;
    cy.nodes().forEach(function(n){
      var el = n.data("dom");
      if(el){ el.style.visibility = (n.style("display") === "none") ? "hidden" : ""; }
    });
  }
  // …and the fade/highlight classes live on the canvas element, so mirror them onto
  // the card or focus/search would visibly stop working in card mode.
  function refreshDomFx(){
    if(!domOn) return;
    cy.nodes().forEach(function(n){
      var el = n.data("dom");
      if(!el) return;
      el.classList.toggle("faded", n.hasClass("faded"));
      el.classList.toggle("hi", n.hasClass("hi"));
      el.classList.toggle("found", n.hasClass("found"));
    });
  }

  // Marching ants — cytoscape's own line-dash-offset animation, no extra library.
  // Scoped to the CURRENT SELECTION's edges (never the whole graph: animating every
  // edge of a large graph is the perf trap) and capped. Note this temporarily
  // overrides the confidence line-style encoding (solid/dashed/dotted) on those
  // edges; the original style is restored the moment the ants stop.
  var ANT_CAP = 60, antToken = 0, antEles = null;
  function stopAnts(){
    antToken++;
    if(antEles){
      antEles.stop();
      antEles.removeStyle("line-style line-dash-pattern line-dash-offset");
      antEles = null;
    }
  }
  function marchOne(e, token){
    if(token !== antToken) return;
    e.style("line-dash-offset", 0);
    // one full 2-period slide, restarted — a seamless loop with a stoppable handle
    e.animation({ style: { "line-dash-offset": -20 } }, { duration: 700 })
      .play().promise("complete").then(function(){ marchOne(e, token); }, function(){});
  }
  function marchAnts(edges){
    stopAnts();
    if(!domOn || RM.matches) return;          // preview-only, and never under reduced motion
    if(!edges || !edges.length || edges.length > ANT_CAP) return;
    antEles = edges;
    var token = antToken;
    edges.style({ "line-style": "dashed", "line-dash-pattern": [6, 4], "line-dash-offset": 0 });
    edges.forEach(function(e){ marchOne(e, token); });
  }

  // ===== Overview: two interlocking views — namespace mindmap <-> dependency flow.
  // One graph, two layouts. Clusters mode shows the repo tree as ~N namespace nodes
  // (the structure the user knows) with aggregated namespace→namespace dependency
  // edges; tapping a namespace expands its repos (mindmap drill-in). Flow mode drops
  // the scaffolding and lays the connected repos out by depends-on DIRECTION. =====
  var VIEWMODE = "clusters", nsExpanded = {};
  function nsOf(id){ return String(id).split("/")[0]; }
  function buildOverviewModel(){
    var repos = cy.nodes('[kind = "repo"]'), groups = {}, add = [], agg = {};
    repos.forEach(function(n){ var k = nsOf(n.id()); (groups[k] = groups[k] || []).push(n); });
    Object.keys(groups).forEach(function(ns){
      add.push({ group: "nodes", data: { id: "ns:" + ns, kind: "namespace",
        label: ns + " · " + groups[ns].length, count: groups[ns].length, ns: ns } });
      groups[ns].forEach(function(r){
        r.data("ns", ns);
        add.push({ group: "edges", data: { id: "sc:" + r.id(),
          source: "ns:" + ns, target: r.id(), scaffold: true } });
      });
      nsExpanded[ns] = false;
    });
    // aggregate every cross-namespace repo->repo edge by (src ns, dst ns, relation)
    // so both structural depends_on and runtime flow roll up to the cluster level
    cy.edges().forEach(function(e){
      var a = nsOf(e.data("source")), b = nsOf(e.data("target"));
      if(a === b){ return; }
      var k = a + "" + b + "" + (e.data("relation") || "depends_on");
      agg[k] = (agg[k] || 0) + (e.data("weight") || 1);
    });
    Object.keys(agg).forEach(function(k){
      var p = k.split(""), rel = p[2], n = agg[k];
      var what = rel === "flow"
        ? n + " cross-namespace HTTP " + (n === 1 ? "call" : "calls")
        : n + " cross-namespace package " + (n === 1 ? "dependency" : "dependencies");
      add.push({ group: "edges", data: { id: "agg:" + k, source: "ns:" + p[0],
        target: "ns:" + p[1], relation: rel, confidence: "INFERRED",
        weight: n, aggregated: true,
        context: p[0] + (rel === "flow" ? " calls " : " depends on ") + p[1] + " — " + what } });
    });
    cy.add(add);
  }
  function applyOverview(){
    var clusters = (VIEWMODE === "clusters");
    cy.batch(function(){
      cy.nodes('[kind = "namespace"]').style("display", clusters ? "element" : "none");
      cy.nodes('[kind = "repo"]').forEach(function(r){
        var show = clusters ? !!nsExpanded[r.data("ns")] : (r.data("deg") > 0 || showNodeps);
        r.style("display", show ? "element" : "none");
      });
      cy.edges('[scaffold]').forEach(function(e){
        e.style("display", clusters && nsExpanded[nsOf(e.data("target"))] ? "element" : "none");
      });
      cy.edges('[aggregated]').forEach(function(e){
        var a = e.data("source").slice(3), b = e.data("target").slice(3);
        e.style("display", clusters && !(nsExpanded[a] && nsExpanded[b]) ? "element" : "none");
      });
      cy.edges('[relation = "depends_on"]').not('[aggregated]').forEach(function(e){
        var show = clusters
          ? (nsExpanded[nsOf(e.data("source"))] && nsExpanded[nsOf(e.data("target"))])
          : true;
        e.style("display", show ? "element" : "none");
      });
    });
    cy.emit("clake-vis");   // visibility changed -> let the minimap refresh its node layer
  }
  function layoutClusters(){
    var vis = cy.elements().filter(function(el){ return el.visible(); });
    vis.layout({ name: "cose", animate: false, randomize: true, padding: 40,
      nodeOverlap: 24, componentSpacing: 120, gravity: 0.3, numIter: 1200,
      nodeRepulsion: function(){ return 12000; },
      idealEdgeLength: function(e){ return e.data("scaffold") ? 64 : 210; } }).run();
    fitClamped(vis, 45);
    applyLOD(true);
  }
  function layoutFlow(){
    var repoEls = cy.nodes('[kind = "repo"]')
      .add(cy.edges('[relation = "depends_on"]').not('[aggregated]'));
    var comps = repoEls.components().filter(function(c){ return c.nodes().length > 1; });
    comps.sort(function(a, b){ return b.nodes().length - a.nodes().length; });
    // Per-cluster layout, honouring the dropdown. depends_on is hub-and-spoke
    // (libraries everyone uses), not directional chains, so concentric (hub centred)
    // reads best by default; breadthfirst gives a directed-flow attempt on demand.
    var nm = (document.getElementById("layout").value || LAYOUT);
    var per = layoutOpts(nm === "cose" ? "concentric" : nm);
    comps.forEach(function(c){ c.layout(per).run(); });
    if(comps.length){
      var tw = comps.reduce(function(s, c){ return s + c.boundingBox().w + 90; }, 0);
      packRows(comps, Math.max(1500, tw / Math.max(1, Math.round(Math.sqrt(comps.length)))), 100);
    }
    var core = cy.nodes('[kind = "repo"]').filter(function(n){ return n.data("deg") > 0; });
    var iso = cy.nodes('[kind = "repo"]').filter(function(n){ return n.data("deg") === 0; });
    var bb = core.nonempty() ? core.boundingBox() : { x1: 0, y2: 0 };
    var cols = Math.max(1, Math.ceil(Math.sqrt(iso.length || 1)));
    iso.forEach(function(n, i){
      n.position({ x: bb.x1 + (i % cols) * 64, y: bb.y2 + 200 + Math.floor(i / cols) * 64 });
    });
    fitClamped(core, 45);
    applyLOD(true);
  }
  function relayoutOverview(){ if(VIEWMODE === "clusters"){ layoutClusters(); } else { layoutFlow(); } }
  function setMode(m){
    VIEWMODE = m;
    ["clusters", "flow"].forEach(function(k){
      var b = document.getElementById("vm-" + k);
      b.classList.toggle("on", k === m); b.setAttribute("aria-selected", String(k === m));
    });
    var np = document.getElementById("nodeprow");
    if(np){ np.hidden = (m !== "flow") || !noDepCount; }
    cy.elements().removeClass("faded hi found");
    applyOverview(); relayoutOverview();
  }
  // Mindmap drill: expand lays out ONLY this namespace's repos as a local cluster
  // around the (fixed) namespace node — every other namespace stays put, so there's
  // no disorienting global reshuffle. Collapse just hides them. gridKids/expandNs/
  // collapseNs are the reusable primitives: toggleNs adds the click-driven framing
  // animation, while semantic zoom (below) drives the same primitives from zoom level.
  function gridKids(nsNode){
    var ns = nsNode.data("ns");
    var kids = cy.nodes('[kind = "repo"]').filter(function(n){ return n.data("ns") === ns; });
    // compact grid directly beneath the namespace node (a mindmap branch), so it
    // stays tight instead of a wide ring that collides with other namespaces. fit:false
    // is essential — a fitting sub-layout in the zoom path would re-fire "zoom" forever.
    var cx = nsNode.position("x"), cy0 = nsNode.position("y");
    // sp must clear a typical repo-name label's rendered width, not just the node's
    // own glyph circle -- avoidOverlap alone only keeps the circles apart, so two
    // adjacent long names (e.g. "catalog-api" / "auth-service") still ran into each
    // other at the old 72px spacing.
    var cols = Math.max(1, Math.ceil(Math.sqrt(kids.length))), sp = 130;
    var w = cols * sp, h = Math.ceil(kids.length / cols) * sp;
    kids.layout({ name: "grid", animate: false, fit: false, avoidOverlap: true,
      nodeDimensionsIncludeLabels: true, condense: true,
      boundingBox: { x1: cx - w / 2, y1: cy0 + 56, w: w, h: h } }).run();
    return kids;
  }
  function expandNs(nsNode){
    var ns = nsNode.data("ns");
    if(nsExpanded[ns]) return cy.collection();
    nsExpanded[ns] = true;
    applyOverview();
    var kids = gridKids(nsNode);
    applyLOD(true);
    return kids;
  }
  function collapseNs(ns){
    if(!nsExpanded[ns]) return;
    nsExpanded[ns] = false;
    applyOverview();
  }
  function toggleNs(nsNode){
    var ns = nsNode.data("ns");
    cy.elements().removeClass("faded hi found");
    if(!nsExpanded[ns]){
      var kids = expandNs(nsNode);
      var grp = nsNode.union(kids);
      cy.elements().addClass("faded");        // spotlight the opened branch
      grp.union(kids.connectedEdges()).removeClass("faded");
      fitClampedAnimated(grp, 55, 350);
    } else {
      collapseNs(ns);
      var nsVis = cy.nodes('[kind = "namespace"]').filter(function(n){ return n.visible(); });
      fitClampedAnimated(nsVis, 45, 350);
    }
  }

  // Level-of-detail labels: dense graphs overlap their text into illegibility, so
  // below a readable zoom we keep labels only on the higher-degree hubs. Re-styling
  // every node on each zoom tick is wasteful, so recompute only when zoom crosses a
  // tier boundary, coalesced via requestAnimationFrame. Namespace nodes are exempt.
  var lodTier = -2, lodRAF = 0;
  function lodThreshold(z){ return z >= 0.9 ? 0 : (z >= 0.45 ? 3 : 8); }
  function applyLOD(force){
    var thr = lodThreshold(cy.zoom());
    if(thr === lodTier && !force){ return; }
    lodTier = thr;
    cy.batch(function(){
      cy.nodes('[kind != "namespace"]').forEach(function(n){
        n[(n.data("deg") || 0) < thr ? "addClass" : "removeClass"]("dim-label");
      });
    });
  }

  // Semantic zoom (overview clusters only): as you zoom into a region, the namespace
  // clusters whose blob is on-screen expand into their repos; zoom back out and they
  // collapse. A hysteresis gap (SZ_COLLAPSE..SZ_EXPAND) prevents flapping; we NEVER
  // fit/center/animate here and guard re-entrancy, so zoom can't feed back on itself.
  // Flags are flipped first, then ONE applyOverview + the grid layouts (batched).
  var SZ_EXPAND = 0.5, SZ_COLLAPSE = 0.32, szRAF = 0, szGuard = false;
  function applySemanticZoom(){
    if(szGuard || !(OVERVIEW && VIEWMODE === "clusters")){ return; }
    szGuard = true;
    var z = cy.zoom(), ext = cy.extent(), toGrid = [], changed = false;
    cy.nodes('[kind = "namespace"]').forEach(function(ns){
      var nm = ns.data("ns"), open = !!nsExpanded[nm];
      if(z >= SZ_EXPAND && !open){
        var bb = ns.boundingBox();
        if(bb.x2 >= ext.x1 && bb.x1 <= ext.x2 && bb.y2 >= ext.y1 && bb.y1 <= ext.y2){
          nsExpanded[nm] = true; toGrid.push(ns); changed = true;
        }
      } else if(z < SZ_COLLAPSE && open){
        nsExpanded[nm] = false; changed = true;
      }
    });
    if(changed){
      applyOverview();
      toGrid.forEach(function(ns){ gridKids(ns); });
      applyLOD(true);
    }
    szGuard = false;
  }

  if(OVERVIEW){
    buildOverviewModel();
    document.getElementById("viewmodes").hidden = false;
    document.getElementById("vm-clusters").onclick = function(){ setMode("clusters"); };
    document.getElementById("vm-flow").onclick = function(){ setMode("flow"); };
    setMode("clusters");
  } else {
    applyRenderMode(LAYOUT);
    runLayout(LAYOUT);
  }
  // Belt-and-braces for the iframe/hidden-panel case: if the container already had
  // size, the ResizeObserver may not tick — so reframe once after first paint too
  // (idempotent; the initialFramed guard keeps it to a single re-fit).
  requestAnimationFrame(frameInitial);
  setTimeout(frameInitial, 250);

  // zoom-driven behaviours: LOD labels (every graph) + semantic cluster zoom
  // (overview only). Separate rAF flags so a burst of zoom events collapses to one
  // recompute each. Registered AFTER the initial layout so we don't react to its fit.
  cy.on("zoom", function(){
    if(!lodRAF){ lodRAF = requestAnimationFrame(function(){ lodRAF = 0; applyLOD(false); }); }
    if(!szRAF){ szRAF = requestAnimationFrame(function(){ szRAF = 0; applySemanticZoom(); }); }
  });

  // cross-page nav (only in a built --site folder): link back to index + overview
  if(SITE){
    var mode = document.getElementById("mode");
    var nav = document.createElement("nav");
    nav.className = "sitenav";
    nav.innerHTML = '<a href="index.html">Index</a><a href="overview.html">Overview</a>';
    mode.parentNode.insertBefore(nav, mode.nextSibling);
  }

  var sel = document.getElementById("layout");
  sel.value = LAYOUT;
  sel.addEventListener("change", function(){
    // enter/leave card rendering FIRST: dagre lays out against node dimensions, and
    // in card mode those come from the rendered HTML, not the stylesheet.
    applyRenderMode(sel.value);
    if(OVERVIEW){ relayoutOverview(); } else { runLayout(sel.value); }
  });

  // ===== Export: PNG (canvas raster) and SVG (vector, card-aware) ==============
  // Two deliberately different paths, because they can see different things:
  //   PNG — cytoscape's own canvas renderer (cy.png), byte-for-byte the call this
  //         page has always made. It renders from the MODEL to an offscreen canvas,
  //         so the HTML cards never participate; in the dagre preview the canvas
  //         node is blanked (.cl-dom) and resized to the card, so the export
  //         temporarily reverts both and restores them. The PNG you get in card
  //         mode is therefore the classic canvas rendering, laid out at the
  //         card-width spacing dagre used — the same picture as before, just airier.
  //   SVG — hand-rolled from cytoscape's geometry. No cytoscape-svg plugin was
  //         vendored: it replays the canvas draw path (canvas2svg), so it
  //         structurally cannot see the cards either. foreignObject can, so the
  //         vector export is the one format that keeps the card look.
  function xmlEsc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
      return { "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&apos;" }[c]; });
  }
  function num(v, d){ var n = parseFloat(v); return isFinite(n) ? n : d; }
  function r2(v){ return Math.round(v * 100) / 100; }
  function xy(p){ return r2(p.x) + "," + r2(p.y); }

  // Run fn with the CANVAS rendering of the nodes restored, then put the card
  // rendering back exactly as cytoscape-dom-node had it. Scoped to the nodes that
  // are actually carded: in LIVE mode expand() can add nodes after card mode was
  // entered, and dom-node skips those (no `dom` data), so they must stay untouched.
  function withCanvasNodes(fn){
    var carded = cy.nodes(".cl-dom");
    if(!domOn || !carded.length){ return fn(); }
    try {
      cy.batch(function(){ carded.removeClass("cl-dom").removeStyle("width height shape"); });
      return fn();
    } finally {
      cy.batch(function(){
        carded.addClass("cl-dom");
        carded.forEach(function(n){
          var el = n.data("dom");
          // exactly what dom-node's own syncNodeSize writes, so the mode is bit-identical
          if(el){ n.style({ width: el.offsetWidth, height: el.offsetHeight, shape: "rectangle" }); }
        });
      });
    }
  }
  function pngDataUri(){
    return withCanvasNodes(function(){ return cy.png({ full:true, scale:2, bg:"#ffffff" }); });
  }

  // The card's look comes from a stylesheet plus CSS custom properties, neither of
  // which travels inside the exported file — so the COMPUTED value of each property
  // is inlined onto the clone. (Reading document.styleSheets instead would throw on
  // a file:// linked stylesheet, which is exactly how a --site build serves app.css.)
  var CARD_PROPS = ["display","flex-direction","align-items","justify-content","gap",
    "box-sizing","width","height","max-width","min-width","padding-top","padding-right",
    "padding-bottom","padding-left","border-top-width","border-right-width",
    "border-bottom-width","border-left-width","border-top-style","border-right-style",
    "border-bottom-style","border-left-style","border-top-color","border-right-color",
    "border-bottom-color","border-left-color","border-top-left-radius",
    "border-top-right-radius","border-bottom-left-radius","border-bottom-right-radius",
    "background-color","background-image","background-size","background-position",
    "background-repeat","color","font-family","font-size","font-weight","line-height",
    "letter-spacing","white-space","overflow","text-overflow","text-transform",
    "box-shadow","opacity","flex"];
  function inlineComputed(src, dst){
    var cs = window.getComputedStyle(src), decl = "";
    CARD_PROPS.forEach(function(p){
      var v = cs.getPropertyValue(p);
      if(v){ decl += p + ":" + v + ";"; }
    });
    dst.setAttribute("style", decl);
    var a = src.children, b = dst.children;
    for(var i = 0; i < a.length && i < b.length; i++){ inlineComputed(a[i], b[i]); }
  }
  // A node's HTML card as well-formed XML. XMLSerializer, never outerHTML: the HTML
  // serializer leaves void elements (<img>, <br>) unclosed, which is not valid XML
  // and would make the whole exported file unparseable.
  function cardXml(el, pad){
    var wrap = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
    var clone = el.cloneNode(true);
    inlineComputed(el, clone);
    // pad the foreignObject and re-inset the card, so the card's drop shadow has
    // room instead of being clipped at the object's edge
    wrap.setAttribute("style", "padding:" + pad + "px;box-sizing:border-box");
    wrap.appendChild(clone);
    return new XMLSerializer().serializeToString(wrap);
  }

  function edgeGeom(e){
    var p1, p2, cps = [];
    try { p1 = e.sourceEndpoint(); p2 = e.targetEndpoint(); } catch(err){ p1 = p2 = null; }
    if(!p1 || !p2){ p1 = e.source().position(); p2 = e.target().position(); }
    try { cps = e.controlPoints() || []; } catch(err){ cps = []; }   // straight-line fallback
    var d = "M" + xy(p1), mid = { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 };
    if(!cps.length){
      d += "L" + xy(p2);
    } else {
      for(var i = 0; i < cps.length; i++){
        // cytoscape draws its bezier as quadratics through the control points, with
        // the midpoint between consecutive controls as the implied on-curve point
        var end = (i === cps.length - 1) ? p2
                : { x: (cps[i].x + cps[i + 1].x) / 2, y: (cps[i].y + cps[i + 1].y) / 2 };
        d += "Q" + xy(cps[i]) + " " + xy(end);
      }
      if(cps.length === 1){   // exact midpoint of a quadratic
        mid = { x: (p1.x + 2 * cps[0].x + p2.x) / 4, y: (p1.y + 2 * cps[0].y + p2.y) / 4 };
      } else {
        mid = cps[Math.floor(cps.length / 2)];
      }
    }
    return { d: d, p1: p1, p2: p2, mid: mid };
  }

  var SVG_PAD = 40, CARD_PAD = 12, SVG_LABEL_MAX = 28;
  function svgText(){
    var nodes = cy.nodes().filter(function(n){ return n.visible(); });
    var edges = cy.edges().filter(function(e){ return e.visible(); });
    var els = nodes.union(edges);
    var bb = els.nonempty() ? els.boundingBox()
                            : { x1: 0, y1: 0, x2: 1, y2: 1, w: 1, h: 1 };
    var x = bb.x1 - SVG_PAD, y = bb.y1 - SVG_PAD;
    var w = Math.max(1, bb.w + 2 * SVG_PAD), h = Math.max(1, bb.h + 2 * SVG_PAD);
    var bg = cssVar("--surface-solid") || "#ffffff";
    var labelColor = cssVar("--canvas-label") || "#0E2A33";
    var ff = cssVar("--ff") || "sans-serif";
    var markers = {}, defs = "";
    function marker(color){
      if(!markers[color]){
        var id = "arw" + (Object.keys(markers).length + 1);
        markers[color] = id;
        defs += '<marker id="' + id + '" viewBox="0 0 8 8" refX="8" refY="4"'
          + ' markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto">'
          + '<path d="M0,0 L8,4 L0,8 z" fill="' + xmlEsc(color) + '"/></marker>';
      }
      return markers[color];
    }
    function text(tx, ty, size, fill, s, extra){
      return '<text x="' + r2(tx) + '" y="' + r2(ty) + '" text-anchor="middle" font-size="'
        + r2(size) + '" font-family="' + xmlEsc(ff) + '" fill="' + xmlEsc(fill) + '"'
        + (extra || "") + ">" + xmlEsc(s) + "</text>";
    }

    var eout = "";
    edges.forEach(function(e){
      var g = edgeGeom(e), color = edgeColor(e);
      var ls = String(e.style("line-style") || "solid");
      var dash = ls === "dashed" ? ' stroke-dasharray="6,4"'
               : (ls === "dotted" ? ' stroke-dasharray="1,3"' : "");
      // the overview's faint "contains" spokes carry no arrowhead on canvas either
      var head = String(e.style("target-arrow-shape")) === "none"
        ? "" : ' marker-end="url(#' + marker(color) + ')"';
      eout += '<path class="edge" data-id="' + xmlEsc(e.id()) + '" d="' + g.d
        + '" fill="none" stroke="' + xmlEsc(color) + '" stroke-width="'
        + r2(num(e.style("width"), 1)) + '" opacity="' + r2(num(e.style("opacity"), 1))
        + '"' + dash + head + "/>";
      // aggregated overview edges are labelled with their rolled-up weight, not the
      // relation — same as the canvas stylesheet's edge[aggregated] rule
      var agg = !!e.data("aggregated");
      var lab = agg ? String(e.data("weight") || "") : (edgeLabel(e) || "");
      if(lab){
        var deg = Math.atan2(g.p2.y - g.p1.y, g.p2.x - g.p1.x) * 180 / Math.PI;
        if(deg > 90 || deg < -90){ deg += 180; }   // keep labels upright
        eout += text(g.mid.x, g.mid.y - 3, agg ? 10 : 7, labelColor, lab,
          ' transform="rotate(' + r2(deg) + ' ' + xy(g.mid) + ')"'
          + (agg ? ' font-weight="600"' : ""));
      }
    });

    var nout = "";
    nodes.forEach(function(n){
      var pos = n.position(), d = n.data();
      var card = domOn ? d.dom : null;
      var head = '<g class="node" data-id="' + xmlEsc(n.id()) + '"';
      if(card){
        var cw = card.offsetWidth, ch = card.offsetHeight;
        nout += head + ' data-render="card"><foreignObject x="'
          + r2(pos.x - cw / 2 - CARD_PAD) + '" y="' + r2(pos.y - ch / 2 - CARD_PAD)
          + '" width="' + (cw + 2 * CARD_PAD) + '" height="' + (ch + 2 * CARD_PAD) + '">'
          + cardXml(card, CARD_PAD) + "</foreignObject></g>";
        return;
      }
      var nw = n.width(), nh = n.height();
      var shape = String(n.style("shape") || "ellipse");
      var bw = num(n.style("border-width"), 0);
      var stroke = bw > 0
        ? ' stroke="' + xmlEsc(n.style("border-color") || bg) + '" stroke-width="' + r2(bw) + '"'
        : "";
      var fill = ' fill="' + xmlEsc(n.style("background-color") || DEFAULT_COLOR)
        + '" fill-opacity="' + r2(num(n.style("background-opacity"), 1)) + '"';
      nout += head + ' opacity="' + r2(num(n.style("opacity"), 1)) + '">';
      if(shape.indexOf("rectangle") >= 0){
        nout += '<rect x="' + r2(pos.x - nw / 2) + '" y="' + r2(pos.y - nh / 2) + '" width="'
          + r2(nw) + '" height="' + r2(nh) + '" rx="' + (shape.indexOf("round") === 0 ? 10 : 0)
          + '"' + fill + stroke + "/>";
      } else {
        nout += '<ellipse cx="' + r2(pos.x) + '" cy="' + r2(pos.y) + '" rx="' + r2(nw / 2)
          + '" ry="' + r2(nh / 2) + '"' + fill + stroke + "/>";
      }
      // the very glyph the canvas paints — a data URI, so the file stays offline
      var icon = (d.kind === "repo" && LANG_ICONS[d.lang]) ? LANG_ICONS[d.lang] : ICONS[d.kind];
      if(icon){
        var iw = nw * 0.58, ih = nh * 0.58;
        nout += '<image href="' + xmlEsc(icon) + '" xlink:href="' + xmlEsc(icon) + '" x="'
          + r2(pos.x - iw / 2) + '" y="' + r2(pos.y - ih / 2) + '" width="' + r2(iw)
          + '" height="' + r2(ih) + '"/>';
      }
      var lab = d.label || "";
      if(lab.length > SVG_LABEL_MAX){ lab = lab.slice(0, SVG_LABEL_MAX - 1) + "…"; }
      if(lab && num(n.style("text-opacity"), 1) > 0.01){   // honours the LOD dimming
        var fs = num(n.style("font-size"), 9);
        var ty = String(n.style("text-valign")) === "center"
          ? pos.y + fs * 0.35 : pos.y + nh / 2 + fs + 2;
        nout += text(pos.x, ty, fs, labelColor, lab);
      }
      nout += "</g>";
    });

    return '<?xml version="1.0" encoding="UTF-8"?>\n'
      + '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
      + ' version="1.1" width="' + r2(w) + '" height="' + r2(h) + '" viewBox="'
      + r2(x) + " " + r2(y) + " " + r2(w) + " " + r2(h) + '">'
      + "<title>" + xmlEsc(document.title) + "</title>"
      + '<rect x="' + r2(x) + '" y="' + r2(y) + '" width="' + r2(w) + '" height="' + r2(h)
      + '" fill="' + xmlEsc(bg) + '"/>'
      + "<defs>" + defs + "</defs>"
      + '<g class="edges">' + eout + "</g>"
      + '<g class="nodes">' + nout + "</g></svg>";
  }

  function saveUri(uri, name){
    var a = document.createElement("a");
    a.href = uri; a.download = name; a.click();
  }
  // Both exporters are reachable from the console (and from tests) without going
  // through a download the caller then has to intercept.
  window.clExport = { pngDataUri: pngDataUri, svgText: svgText };

  // toolbar
  document.getElementById("fit").onclick = function(){ reframe(); };
  document.getElementById("png").onclick = function(){
    saveUri(pngDataUri(), "contextlake-graph.png");
  };
  var svgBtn = document.getElementById("svg");
  if(svgBtn){
    svgBtn.onclick = function(){
      var url = URL.createObjectURL(new Blob([svgText()], { type: "image/svg+xml;charset=utf-8" }));
      saveUri(url, "contextlake-graph.svg");
      setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
    };
  }
  document.getElementById("reset").onclick = function(){
    cy.elements().removeClass("faded hi found");
    stopAnts(); refreshDomFx();
    hidden = {}; hiddenRel = {}; showNodeps = false;
    var sn = document.getElementById("shownodeps");
    if(sn){ sn.checked = false; }
    document.getElementById("search").value = "";
    hideInfo(); syncLegend();
    if(OVERVIEW){
      Object.keys(nsExpanded).forEach(function(k){ nsExpanded[k] = false; });
      setMode("clusters");
    } else {
      applyFilter(); reframe();
    }
  };

  // legends = kind filter (nodes) + relationship filter (edges)
  var hidden = {}, hiddenRel = {}, showNodeps = false;
  function applyFilter(){
    cy.nodes().forEach(function(n){
      var off = hidden[n.data("kind")] || (isNoDep(n) && !showNodeps);
      n.style("display", off ? "none" : "element");
    });
    cy.edges().forEach(function(e){
      e.style("display", hiddenRel[e.data("relation")] ? "none" : "element");
    });
    syncDomVisibility();
    cy.emit("clake-vis");   // visibility changed -> let the minimap refresh its node layer
  }
  function syncLegend(){
    document.querySelectorAll("#legend .lg").forEach(function(el){
      el.classList.toggle("off", !!hidden[el.getAttribute("data-kind")]);
    });
    document.querySelectorAll("#edgelegend .lg").forEach(function(el){
      el.classList.toggle("off", !!hiddenRel[el.getAttribute("data-rel")]);
    });
  }
  document.querySelectorAll("#legend .lg").forEach(function(el){
    el.addEventListener("click", function(){
      var k = el.getAttribute("data-kind");
      hidden[k] = !hidden[k]; applyFilter(); syncLegend();
    });
  });
  document.querySelectorAll("#edgelegend .lg").forEach(function(el){
    el.addEventListener("click", function(){
      var r = el.getAttribute("data-rel");
      hiddenRel[r] = !hiddenRel[r]; applyFilter(); syncLegend();
    });
  });

  // no-dependency repos: hidden by default in the overview, revealable via a toggle.
  // no-dep toggle (flow mode only): the overview controller governs visibility, so
  // route the change through applyOverview rather than the kind-filter.
  var shownodeps = document.getElementById("shownodeps");
  if(OVERVIEW && noDepCount){
    document.getElementById("nodepn").textContent = noDepCount;
    shownodeps.addEventListener("change", function(){
      showNodeps = shownodeps.checked;
      applyOverview(); relayoutOverview();
    });
  }

  // search -> highlight + frame matches (reveals hidden repos so every repo stays
  // findable; clearing restores the mode's visibility state)
  function restoreVisibility(){
    if(OVERVIEW){ applyOverview(); } else { applyFilter(); }
    refreshDomFx();
  }
  var search = document.getElementById("search");
  search.addEventListener("input", function(){
    var q = search.value.trim().toLowerCase();
    cy.nodes().removeClass("found");
    if(!q){ restoreVisibility(); return; }
    var hits = cy.nodes().filter(function(n){
      return (n.data("label")||"").toLowerCase().indexOf(q) >= 0
          || (n.data("qn")||"").toLowerCase().indexOf(q) >= 0;
    });
    hits.style("display", "element");
    hits.addClass("found");
    syncDomVisibility(); refreshDomFx();
    if(hits.length){ fitClampedAnimated(hits, 90, 300); }
  });

  // hover tooltip
  var tip = document.getElementById("tip");
  cy.on("mouseover", "node", function(e){
    var n = e.target;
    n.addClass("lbl-on");   // always reveal the hovered node's label, even when LOD-dimmed
    tip.textContent = (n.data("label")||"") + "  \u00b7  " + (n.data("kind")||"");
    tip.style.display = "block";
  });
  cy.on("mousemove", function(e){
    if(tip.style.display === "block"){
      tip.style.left = (e.renderedPosition.x + 12) + "px";
      tip.style.top  = (e.renderedPosition.y + 12) + "px";
    }
  });
  cy.on("mouseout", "node", function(e){ e.target.removeClass("lbl-on"); tip.style.display = "none"; });
  cy.on("mouseover", "edge", function(e){
    var d = e.target.data();
    if(d.aggregated){
      tip.textContent = d.context;
    } else {
      var prov = d.prov_file
        ? "  \u00b7  " + d.prov_file + (d.prov_line ? ":" + d.prov_line : "") : "";
      tip.textContent = d.relation + "  \u00b7  " + d.confidence + prov;
    }
    tip.style.display = "block";
  });
  cy.on("mouseout", "edge", function(){ tip.style.display = "none"; });

  // selection -> focus + detail panel (nodes AND edges)
  var info = document.getElementById("info");
  function esc(s){ return (s == null ? "" : ("" + s)).replace(/[&<>"]/g, function(c){
    return { "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]; }); }
  function row(k, v){
    return (v === undefined || v === null || v === "")
      ? "" : "<dt>" + k + "</dt><dd>" + esc(v) + "</dd>";
  }
  // List the node's relationships in the inspector; each neighbour is clickable
  // (data-id) to jump to it — in-view navigation between connected entities.
  function connList(n, all){
    var es = n.connectedEdges(), id = n.id(), cap = all ? es.length : 14;
    if(!es.length) return "";
    var rows = es.sort(function(a, b){ return (b.data("weight")||1) - (a.data("weight")||1); });
    var items = "";
    rows.slice(0, cap).forEach(function(ed){
      var out = ed.data("source") === id;
      var other = out ? ed.target() : ed.source();
      var hue = REL_COLORS[ed.data("relation")] || DEFAULT_EDGE_COLOR;
      items += '<li><span class="rdot" style="background:' + hue + '"></span>'
        + '<span class="rl">' + esc(ed.data("relation")) + (out ? " →" : " ←") + "</span>"
        + '<span class="rn" data-id="' + esc(other.id()) + '">'
        + esc(other.data("label") || other.id()) + "</span></li>";
    });
    if(es.length > cap){ items += '<li class="rmore">+' + (es.length - cap) + " more — show all</li>"; }
    return '<div class="conns"><h3>connections <span class="cc">' + es.length
      + "</span></h3><ul>" + items + "</ul></div>";
  }
  var curNode = null;  // node whose detail is open, so "show all" can re-render it
  function showInfo(n, allConns){
    curNode = n;
    var d = n.data();
    var fileline = d.file ? (d.file + (d.line ? ":" + d.line : "")) : "";
    info.innerHTML = "<h2>" + esc(d.label || d.id) + "</h2><dl>"
      + row("kind", d.kind) + row("repo", d.repo) + row("qualified", d.qn)
      + row("file", fileline) + row("nodes", d.count) + row("degree", d.deg) + "</dl>"
      + (SITE && d.href ? '<a class="gopage" href="' + esc(d.href)
          + '">Open this repo’s graph →</a>' : "")
      + connList(n, allConns)
      + (LIVE ? '<div class="hint">tap any node to expand its neighbours</div>' : "");
    openInspector();
  }
  function showEdgeInfo(ed){
    var d = ed.data();
    var c = CONF_META[d.confidence] || CONF_META.EXTRACTED;  // [label, dot, blurb]
    var hue = REL_COLORS[d.relation] || DEFAULT_EDGE_COLOR;
    var sN = cy.getElementById(d.source), tN = cy.getElementById(d.target);
    var prov = d.prov_file ? (d.prov_file + (d.prov_line ? ":" + d.prov_line : "")) : "";
    info.innerHTML =
      '<h2><span class="rel-chip" style="background:' + hue + '">'
      + esc(d.relation) + "</span></h2>"
      + '<div class="edge-flow">' + esc(sN.data("label"))
      + " \u2192 " + esc(tN.data("label")) + "</div>"
      + '<div class="trust"><span class="dot" style="background:' + c[1] + '"></span>'
      + "<b>" + esc(c[0]) + "</b><span class=\"blurb\">" + esc(c[2]) + "</span></div>"
      + "<dl>" + row("context", d.context) + row("weight", d.weight)
      + row("source", prov) + row("verified", d.verified_at) + "</dl>"
      + (prov ? '<button class="copy-prov" data-prov="' + esc(prov)
                + '">copy file:line</button>' : "");
    openInspector();
  }
  info.addEventListener("click", function(ev){
    var b = ev.target.closest && ev.target.closest(".copy-prov");
    if(b && navigator.clipboard){ navigator.clipboard.writeText(b.getAttribute("data-prov")); return; }
    if(ev.target.closest && ev.target.closest(".rmore")){
      if(curNode){ showInfo(curNode, true); }
      return;
    }
    var rn = ev.target.closest && ev.target.closest(".rn");
    if(rn){
      var node = cy.getElementById(rn.getAttribute("data-id"));
      if(node && node.nonempty()){ focus(node); showInfo(node); frameOn(node.closedNeighborhood()); }
    }
  });
  function afterResize(){ cy.resize(); }  // ResizeObserver also catches the post-transition size
  function openInspector(){ document.body.dataset.inspect = "open"; afterResize(); }
  function hideInfo(){ document.body.dataset.inspect = "closed"; afterResize(); }
  // After the inspector slide settles, re-fit the canvas onto the selection so it
  // reflows AND stays legible (plain cy.resize() keeps the old zoom/pan -> clipped).
  function frameOn(eles){
    if(!eles || !eles.nonempty()) return;
    setTimeout(function(){
      cy.resize();
      fitClampedAnimated(eles, 80, 300);
    }, 210);
  }

  function focus(node){
    cy.elements().addClass("faded").removeClass("hi");
    node.closedNeighborhood().removeClass("faded").addClass("hi");
    refreshDomFx();
    marchAnts(node.connectedEdges());   // no-op outside the dagre preview
  }
  cy.on("tap", function(e){
    if(e.target === cy){
      cy.elements().removeClass("faded hi"); hideInfo();
      refreshDomFx(); stopAnts();
    }
  });
  cy.on("tap", "node", function(e){
    // overview clusters mode: tapping a namespace drills in/out (mindmap), not focus
    if(e.target.data("kind") === "namespace"){ toggleNs(e.target); return; }
    focus(e.target); showInfo(e.target);
    // overview repo nodes navigate via the inspector link, never /neighbors-expand
    if(LIVE && !OVERVIEW){ expand(e.target.id()); }
    else { frameOn(e.target.closedNeighborhood()); }
  });
  cy.on("tap", "edge", function(e){
    var ed = e.target;
    cy.elements().addClass("faded").removeClass("hi");
    ed.connectedNodes().add(ed).removeClass("faded").addClass("hi");
    refreshDomFx();
    marchAnts(ed);
    showEdgeInfo(ed);
    frameOn(ed.connectedNodes());
  });

  // ===== Minimap: a custom radar (no cytoscape extension — offline, zero deps).
  // Two layers: a STATIC bitmap of every visible node dot (recomputed only when the
  // layout changes) and a DYNAMIC viewport rectangle redrawn on pan/zoom. Click or
  // drag inside it to recentre the main view — navigate a big graph without 10x scroll.
  (function(){
    var mm = document.getElementById("minimap");
    if(!mm){ return; }
    if(!cy.nodes().length){ mm.style.display = "none"; return; }
    var mctx = mm.getContext("2d");
    var W = mm.width, H = mm.height, PAD = 7;
    var off = document.createElement("canvas"); off.width = W; off.height = H;
    var octx = off.getContext("2d");
    var tf = null, dynRAF = 0, statRAF = 0;   // tf = {s, ox, oy} model->minimap transform

    function visBB(){
      var els = cy.elements(":visible");
      return els.nonempty() ? els.boundingBox()
                            : { x1: 0, y1: 0, x2: 1, y2: 1, w: 1, h: 1 };
    }
    function drawStatic(){
      var bb = visBB();
      var s = Math.min((W - 2 * PAD) / Math.max(bb.w, 1), (H - 2 * PAD) / Math.max(bb.h, 1));
      tf = { s: s,
             ox: PAD + ((W - 2 * PAD) - bb.w * s) / 2 - bb.x1 * s,
             oy: PAD + ((H - 2 * PAD) - bb.h * s) / 2 - bb.y1 * s };
      octx.clearRect(0, 0, W, H);
      // draw EVERY visible node — in collapsed overview the only visible nodes are the
      // namespace clusters, so skipping them would leave the minimap blank (the very
      // "too many nodes" case this is for). Namespaces take the brand lake colour.
      cy.nodes(":visible").forEach(function(n){
        var ns = n.data("kind") === "namespace";
        var p = n.position(), r = ns ? 2.5 : 1.6;
        octx.fillStyle = ns ? "#137A8B" : (COLORS[n.data("kind")] || DEFAULT_COLOR);
        octx.fillRect(p.x * s + tf.ox - r, p.y * s + tf.oy - r, r * 2, r * 2);
      });
      drawDynamic();
    }
    function drawDynamic(){
      if(!tf){ return; }
      mctx.clearRect(0, 0, W, H);
      mctx.drawImage(off, 0, 0);
      var e = cy.extent();
      mctx.strokeStyle = cssVar("--brand") || "#2BB3A3";
      mctx.lineWidth = 1.5;
      mctx.strokeRect(e.x1 * tf.s + tf.ox, e.y1 * tf.s + tf.oy,
                      (e.x2 - e.x1) * tf.s, (e.y2 - e.y1) * tf.s);
    }
    function schedule(which){
      if(which === "static"){
        if(statRAF){ return; }
        statRAF = requestAnimationFrame(function(){ statRAF = 0; drawStatic(); });
      } else {
        if(dynRAF){ return; }
        dynRAF = requestAnimationFrame(function(){ dynRAF = 0; drawDynamic(); });
      }
    }
    function panToEvent(evt){
      if(!tf){ return; }
      var r = mm.getBoundingClientRect();
      var mx = (evt.clientX - r.left - tf.ox) / tf.s;
      var my = (evt.clientY - r.top - tf.oy) / tf.s;
      cy.pan({ x: cy.width() / 2 - mx * cy.zoom(), y: cy.height() / 2 - my * cy.zoom() });
    }
    var dragging = false;
    mm.addEventListener("mousedown", function(e){ dragging = true; panToEvent(e); e.preventDefault(); });
    window.addEventListener("mousemove", function(e){ if(dragging){ panToEvent(e); } });
    window.addEventListener("mouseup", function(){ dragging = false; });

    cy.on("pan zoom resize", function(){ schedule("dynamic"); });
    cy.on("layoutstop dragfree add remove clake-vis", function(){ schedule("static"); });
    var themeBtn = document.getElementById("theme");
    if(themeBtn){
      var prev = themeBtn.onclick;
      themeBtn.onclick = function(){ if(prev){ prev.call(this); } drawDynamic(); };
    }
    drawStatic();
  })();

  function expand(id){
    var cyEl = document.getElementById("cy");
    cyEl.classList.add("loading");
    fetch("/neighbors?id=" + encodeURIComponent(id) + "&direction=both")
      .then(function(r){ return r.json(); })
      .then(function(p){
        cyEl.classList.remove("loading");
        var added = [];
        p.nodes.forEach(function(n){
          if(cy.getElementById(n.id).empty()){
            added.push({ group:"nodes", data:{ id:n.id, label:(n.name||n.id),
              kind:(n.kind||""), repo:(n.repo||""), qn:(n.qualified_name||""),
              file:(n.file||""), line:n.line } });
          }
        });
        p.edges.forEach(function(ed){
          var eid = ed.src + "->" + ed.dst + ":" + ed.relation;
          if(cy.getElementById(eid).empty()){
            added.push({ group:"edges", data:{ id:eid, source:ed.src, target:ed.dst,
              relation:ed.relation, confidence:(ed.confidence||"EXTRACTED"),
              context:(ed.context||""), weight:(ed.weight==null?1.0:ed.weight),
              prov_file:(ed.prov_file||""), prov_line:ed.prov_line,
              verified_at:(ed.verified_at||"") } });
          }
        });
        if(added.length){
          cy.add(added);
          cy.nodes().forEach(function(n){ n.data("deg", n.degree(false)); });
          applyFilter();
          runLayout(sel.value || LAYOUT);
        }
      })
      .catch(function(){ cyEl.classList.remove("loading"); });
  }
