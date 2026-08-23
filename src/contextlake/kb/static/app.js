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
  function themeName(){ return document.body.dataset.theme === "dark" ? "dark" : "light"; }
  var RM = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  function dur(ms){ return RM.matches ? 0 : ms; }   // collapse motion to instant under reduced-motion

  // The cytoscape stylesheet is rebuilt on theme change: CSS variables can't reach
  // canvas pixels, so node-label / highlight text colours are re-read here. (node.hi
  // is a RING, not a background swap, so it reads on both light and dark themes.)
  function graphStyle(){
    var label = cssVar("--canvas-label") || "#0E2A33";
    var surf = cssVar("--surface-solid") || "#ffffff";
    var ink = EDGE_INK[themeName()] || EDGE_INK.light;
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
          // The border, not the fill, is what makes a node perceivable (1.4.11): the
          // fills come from the shared kind registry and 17 of the 40 sit under 3:1
          // against the light canvas. The old value painted the border in
          // --surface-solid, i.e. white in light theme (1.00-1.16:1 -- it added
          // nothing) and NAVY in dark theme (1.03:1 -- it was not a boundary at all).
          "border-width": 1.2, "border-color": ink.node } },
      { selector: "edge", style: {
          "line-color": edgeColor, "target-arrow-color": edgeColor,
          "width": "mapData(weight, 1, 10, 0.8, 4.5)",
          "target-arrow-shape": "triangle", "arrow-scale": 0.7, "curve-style": "bezier",
          // labelled flows: relation (+ path/package/topic) on architectural edges only
          "label": edgeLabel, "font-size": 7, "color": label,
          "text-rotation": "autorotate", "text-margin-y": -3,
          "text-background-color": surf, "text-background-opacity": 0.85,
          "text-background-padding": 2, "text-background-shape": "roundrectangle" } },
      // Confidence is line STYLE only. It used to be style + opacity, and the opacity
      // was the part that made 1.4.11 unreachable: composited at 0.45 over a light
      // canvas, no hue clears 3:1 -- pure black tops out at about 3.3:1. Style alone
      // is the encoding the legend key already documents, and it costs no contrast.
      { selector: 'edge[confidence = "EXTRACTED"]',
        style: { "line-style": "solid", "opacity": 1 } },
      { selector: 'edge[confidence = "INFERRED"]',
        style: { "line-style": "dashed", "opacity": 1 } },
      { selector: 'edge[confidence = "AMBIGUOUS"]',
        style: { "line-style": "dotted", "opacity": 1 } },
      { selector: ".faded", style: {
          "opacity": (parseFloat(cssVar("--faded-opacity")) || 0.1), "text-opacity": 0 } },
      // level-of-detail labels: at low zoom only high-degree hubs keep their text
      // (driven by applyLOD). dim-label hides; lbl-on (hover) and hi/found (highlight,
      // search) force it back on. lbl-on sits AFTER dim-label so it wins on a tie.
      { selector: "node.dim-label", style: { "text-opacity": 0 } },
      { selector: "node.lbl-on", style: { "text-opacity": 1 } },
      // hi/found are state, so their rings carry the same 3:1 duty as the base border
      // -- hence theme-aware hues rather than the two fixed brand colours, which sat
      // at 2.24:1 and 1.64:1 against the light canvas (a highlighted node had a
      // *weaker* outline than an unhighlighted one).
      { selector: "node.hi", style: { "border-width": 3, "border-color": ink.hi,
          "text-opacity": 1, "z-index": 99 } },
      { selector: "node.found", style: { "border-width": 4, "border-color": ink.found,
          "text-opacity": 1, "z-index": 100 } },
      { selector: "edge.hi", style: { "width": 2.2, "opacity": 1,
          "label": "data(relation)", "font-size": 7, "color": label,
          "text-rotation": "autorotate", "text-background-color": surf,
          "text-background-opacity": 0.9, "z-index": 99 } },
      // overview namespace mindmap: cluster nodes, faint "contains" spokes, and
      // aggregated namespace-to-namespace dependency edges
      { selector: 'node[kind = "namespace"]', style: {
          "shape": "round-rectangle", "background-color": ink.ns,
          "background-opacity": 0.13, "border-width": 1.5, "border-color": ink.ns,
          "label": "data(label)", "font-size": 12, "font-weight": 600, "color": label,
          "text-valign": "center", "text-halign": "center", "text-wrap": "wrap",
          "text-max-width": 130, "text-margin-y": 0,
          "width": "mapData(count, 1, 120, 46, 130)",
          "height": "mapData(count, 1, 120, 46, 130)", "z-index": 2 } },
      // The namespace spokes stay subordinate through WIDTH, not opacity: 0.4 opacity
      // put them at 1.2:1 in light theme, and they carry real structure (which repos
      // are in which namespace), so they are not decoration.
      { selector: 'edge[scaffold]', style: {
          "line-color": ink.scaffold, "width": 0.9, "target-arrow-shape": "none",
          "opacity": 1, "curve-style": "straight" } },
      // "dagre (preview)" only: the canvas node is blanked so the real HTML card
      // (cytoscape-dom-node) is all you see. Never applied under any other layout.
      { selector: "node.cl-dom", style: {
          "background-opacity": 0, "background-image": "none",
          "border-width": 0, "label": "" } },
      { selector: 'edge[aggregated]', style: {
          "width": "mapData(weight, 1, 20, 1.6, 7)", "opacity": 1,
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
  // "1 edges" was showing on any single-edge graph.
  function plural(n, word){ return n + " " + word + (n === 1 ? "" : "s"); }
  document.getElementById("meta").textContent =
    plural(cy.nodes().length, "node") + " \u00b7 " + plural(cy.edges().length, "edge")
    + (noDepCount ? " \u00b7 " + noDepCount + " with no detected dependency" : "")
    // Folding removes the majority of nodes on a large repo page. Saying so is not a
    // nicety: an unstated reduction reads as "this is the whole graph".
    + (META.folded_leaves
        ? " \u00b7 " + META.folded_leaves + (META.folded_leaves === 1
            ? " leaf folded into its container"
            : " leaves folded into their containers")
        : "");
  if(!cy.nodes().length){ document.getElementById("empty").classList.add("show"); }
  // honesty: when the view was capped, say so (never imply completeness)
  if(META.truncated){
    var tb = document.getElementById("trunc");
    tb.textContent = "\u26a0 showing " + cy.nodes().length
      + (META.total ? " of " + META.total : "") + " \u2014 truncated; raise --max-nodes";
    tb.classList.add("show");
  }

  // ONE theme entry point. The theme changes four ways (the button, the OS preference,
  // ?theme= on the initial src, a postMessage from an embedding dashboard) and each has
  // to redo the same work, because the relation palette is per theme now (1.4.11: a hue
  // that clears 3:1 on a near-white canvas cannot also clear it on navy). Hooking one
  // entry point only — as the minimap's themeBtn.onclick wrapper further down does —
  // renders the wrong palette on the other three paths.
  var themeHooks = [];
  function onTheme(fn){ themeHooks.push(fn); }
  function applyTheme(t, force){
    if(t !== "dark" && t !== "light") return;
    if(!force && themeName() === t) return;
    document.body.dataset.theme = t;
    REL_COLORS = (t === "dark") ? REL_COLORS_DARK : REL_COLORS_LIGHT;
    DEFAULT_EDGE_COLOR = DEFAULT_EDGE_COLORS[t];
    cy.style(graphStyle());
    themeHooks.forEach(function(fn){ fn(t); });
  }
  document.getElementById("theme").onclick = function(){
    applyTheme(themeName() === "dark" ? "light" : "dark");
  };
  if(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches){
    applyTheme("dark");
  }
  // Optional dashboard coordination (file://-safe, null-origin tolerant): honor an
  // explicit ?theme=/#theme= on the initial src, and a postMessage from an embedding
  // dashboard — both just ride the shared applyTheme path.
  (function(){
    var m = /[?#&]theme=(dark|light)/.exec(location.href);
    if(m){ applyTheme(m[1]); }
    window.addEventListener("message", function(e){
      var d = e && e.data;
      if(d && d.type === "cl-theme"){ applyTheme(d.theme); }
    });
  })();
  // The relation legend's swatches are painted server-side from the light palette, so
  // they are repainted whenever the palette swaps, or the key stops matching the graph.
  onTheme(function(){
    document.querySelectorAll("#edgelegend .lg.rel").forEach(function(el){
      var i = el.querySelector("i");
      if(i){ i.style.background = REL_COLORS[el.getAttribute("data-rel")] || DEFAULT_EDGE_COLOR; }
    });
    textDirty = true; renderTextView();
  });
  var panel = document.getElementById("panel");
  document.getElementById("navToggle").onclick = function(){
    var c = document.body.dataset.sidebar === "collapsed";
    document.body.dataset.sidebar = c ? "open" : "collapsed";
    // A collapsed rail is 0px wide with overflow hidden, so its controls — including
    // the whole text view — would stay in the tab order while being invisible.
    if(panel){ panel.inert = !c; }
    afterResize();
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
    var t = e.target || {};
    // widened from `tagName === "INPUT"`: a <select>'s typeahead and any
    // contenteditable were firing the single-key shortcuts mid-word
    if(t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT"
       || t.isContentEditable){
      if(e.key === "Escape" && t.blur){ t.blur(); }
      return;
    }
    if(e.key === "/"){ e.preventDefault(); document.getElementById("search").focus(); }
    else if(e.key === "f" || e.key === "F"){ reframe(); }
    else if(e.key === "t" || e.key === "T"){ document.getElementById("theme").click(); }
    else if(e.key === "Escape"){
      // Escape dismisses the tooltip WITHOUT moving the pointer, which is the
      // "dismissable" half of 1.4.13; the hover itself is left alone.
      hideTip(true);
      cy.elements().removeClass("faded hi"); hideInfo(); refreshDomFx(); stopAnts();
    }
  });

  // ===== Canvas keyboard model ================================================
  // The canvas used to be role="application" with no key handling at all: focus went
  // in and nothing moved. Panning and zooming are the minimap's job for a mouse, so
  // they are bound here for a keyboard (2.1.1) — reading and selecting the graph's
  // CONTENT is the text view's job, and Enter jumps to it.
  var PAN_STEP = 90;
  cyEl.addEventListener("keydown", function(e){
    if(e.target !== cyEl) return;              // never swallow keys meant for a child
    var p = { ArrowLeft: [PAN_STEP, 0], ArrowRight: [-PAN_STEP, 0],
              ArrowUp: [0, PAN_STEP], ArrowDown: [0, -PAN_STEP] }[e.key];
    if(p){ e.preventDefault(); cy.panBy({ x: p[0], y: p[1] }); return; }
    if(e.key === "+" || e.key === "="){ e.preventDefault(); zoomStep(1.25); }
    else if(e.key === "-" || e.key === "_"){ e.preventDefault(); zoomStep(1 / 1.25); }
    else if(e.key === "0"){ e.preventDefault(); reframe(); }
    else if(e.key === "Enter"){ e.preventDefault(); openTextView(true); }
  });
  function zoomStep(f){
    cy.zoom({ level: cy.zoom() * f,
              renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }

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
      // aria-pressed, not aria-selected: these are toggle buttons in a group, and
      // there is no tabpanel for an aria-selected tab to control.
      b.classList.toggle("on", k === m); b.setAttribute("aria-pressed", String(k === m));
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
    // background follows the THEME, as svgText() already did. With a per-theme edge
    // palette a hardcoded white ground would paint the dark theme's light hues onto
    // white -- e.g. the pale "publishes" line at 1.7:1 -- so the export would be less
    // readable than the screen it came from.
    var bg = cssVar("--surface-solid") || "#ffffff";
    return withCanvasNodes(function(){ return cy.png({ full:true, scale:2, bg:bg }); });
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
  // aria-pressed polarity: PRESSED == "this kind/relation is currently SHOWN", which is
  // how the page boots (nothing filtered) and what html_render emits, so the
  // server-rendered value and this one cannot drift apart. The class alone left a
  // screen-reader user hearing the same thing whether a whole kind was on screen or not.
  function syncLegend(){
    document.querySelectorAll("#legend .lg").forEach(function(el){
      var off = !!hidden[el.getAttribute("data-kind")];
      el.classList.toggle("off", off);
      el.setAttribute("aria-pressed", String(!off));
    });
    document.querySelectorAll("#edgelegend .lg").forEach(function(el){
      var off = !!hiddenRel[el.getAttribute("data-rel")];
      el.classList.toggle("off", off);
      el.setAttribute("aria-pressed", String(!off));
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

  // ===== Tooltip (1.4.13: dismissable, hoverable, persistent) =================
  // It used to be pointer-events:none and hidden the instant the pointer left the
  // element, so a magnifier user could never travel onto it to read a long provenance
  // string, and there was no focus path at all \u2014 relation, confidence and provenance
  // were mouse-only. Now: the pointer can enter it (a short grace period covers the
  // gap), Escape dismisses it, and the same text is shown when a text-view item takes
  // focus. The tip also names itself to AT via the element it describes.
  var tip = document.getElementById("tip");
  var tipTimer = 0, tipOverTip = false;
  function tipText(s){ tip.textContent = s; }
  function showTipAt(x, y){
    if(tipTimer){ clearTimeout(tipTimer); tipTimer = 0; }
    tip.style.left = x + "px"; tip.style.top = y + "px";
    tip.style.display = "block";
  }
  function hideTip(now){
    if(tipTimer){ clearTimeout(tipTimer); tipTimer = 0; }
    if(now){ tip.style.display = "none"; return; }
    // grace period: the pointer needs time to travel off the trigger and onto the tip
    tipTimer = setTimeout(function(){
      tipTimer = 0;
      if(!tipOverTip){ tip.style.display = "none"; }
    }, 260);
  }
  tip.addEventListener("mouseenter", function(){
    tipOverTip = true;
    if(tipTimer){ clearTimeout(tipTimer); tipTimer = 0; }
  });
  tip.addEventListener("mouseleave", function(){ tipOverTip = false; hideTip(); });
  function nodeTipText(n){
    return (n.data("label") || "") + "  \u00b7  " + (n.data("kind") || "");
  }
  function edgeTipText(ed){
    var d = ed.data();
    if(d.aggregated){ return d.context || ""; }
    var prov = d.prov_file
      ? "  \u00b7  " + d.prov_file + (d.prov_line ? ":" + d.prov_line : "") : "";
    return d.relation + "  \u00b7  " + d.confidence + prov;
  }
  // position from the event, never from the tip's own last coordinates: on the first
  // hover those are 0,0 (top-left flash), and after a text-view focus they are that
  // item's box, so the tip appeared over the sidebar until the next mousemove.
  function tipAtEvent(e){
    var p = e.renderedPosition || { x: 0, y: 0 };
    showTipAt(p.x + 12, p.y + 12);
  }
  cy.on("mouseover", "node", function(e){
    var n = e.target;
    n.addClass("lbl-on");   // always reveal the hovered node's label, even when LOD-dimmed
    tipText(nodeTipText(n));
    tipAtEvent(e);
  });
  cy.on("mousemove", function(e){
    if(tip.style.display === "block" && !tipOverTip){
      tip.style.left = (e.renderedPosition.x + 12) + "px";
      tip.style.top  = (e.renderedPosition.y + 12) + "px";
    }
  });
  cy.on("mouseout", "node", function(e){ e.target.removeClass("lbl-on"); hideTip(); });
  cy.on("mouseover", "edge", function(e){
    tipText(edgeTipText(e.target));
    tipAtEvent(e);
  });
  cy.on("mouseout", "edge", function(){ hideTip(); });

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
      // <button>, not <span>: these navigate between entities, so they have to be
      // reachable and operable without a mouse like everything else in here.
      items += '<li><span class="rdot" style="background:' + hue + '"></span>'
        + '<span class="rl">' + esc(ed.data("relation")) + (out ? " →" : " ←") + "</span>"
        + '<button type="button" class="rn" data-id="' + esc(other.id()) + '">'
        + esc(other.data("label") || other.id()) + "</button></li>";
    });
    if(es.length > cap){
      items += '<li><button type="button" class="rmore">+' + (es.length - cap)
        + " more — show all</button></li>";
    }
    return '<div class="conns"><h3>connections <span class="cc">' + es.length
      + "</span></h3><ul>" + items + "</ul></div>";
  }
  var curNode = null;  // node whose detail is open, so "show all" can re-render it
  function showInfo(n, allConns, fromKeyboard){
    curNode = n;
    var d = n.data();
    var fileline = d.file ? (d.file + (d.line ? ":" + d.line : "")) : "";
    info.innerHTML = "<h2>" + esc(d.label || d.id) + "</h2><dl>"
      + row("kind", d.kind) + row("repo", d.repo) + row("qualified", d.qn)
      + row("file", fileline) + row("nodes", d.count) + row("degree", d.deg)
      + row("folded", d.folded ? (d.folded + (d.folded_kinds ? " (" + d.folded_kinds + ")" : "")) : "")
      + "</dl>"
      + (SITE && d.href ? '<a class="gopage" href="' + esc(d.href)
          + '">Open this repo’s graph →</a>' : "")
      + (n.outgoers("edge").length
          ? '<button type="button" class="tracebtn" id="tracedown">Trace downstream \u2192</button>'
            + '<div class="hint">Follows edge direction as far as it goes, through the graph'
            + (LIVE ? ' currently loaded — expand further to widen it.' : '.') + '</div>'
          : "")
      + connList(n, allConns)
      + (LIVE ? '<div class="hint">select any node to expand its neighbours</div>' : "");
    openInspector(fromKeyboard);
  }
  function showEdgeInfo(ed, fromKeyboard){
    var d = ed.data();
    var c = CONF_META[d.confidence] || CONF_META.EXTRACTED;  // [label, dot, blurb]
    var hue = REL_COLORS[d.relation] || DEFAULT_EDGE_COLOR;
    var sN = cy.getElementById(d.source), tN = cy.getElementById(d.target);
    var prov = d.prov_file ? (d.prov_file + (d.prov_line ? ":" + d.prov_line : "")) : "";
    info.innerHTML =
      '<h2><span class="rel-chip" style="--rel:' + hue + '">'
      + esc(d.relation) + "</span></h2>"
      + '<div class="edge-flow">' + esc(sN.data("label"))
      + " \u2192 " + esc(tN.data("label")) + "</div>"
      + '<div class="trust"><span class="dot" style="background:' + c[1] + '"></span>'
      + "<b>" + esc(c[0]) + "</b><span class=\"blurb\">" + esc(c[2]) + "</span></div>"
      + "<dl>" + row("context", d.context) + row("weight", d.weight)
      + row("source", prov) + row("verified", d.verified_at) + "</dl>"
      + (prov ? '<button class="copy-prov" data-prov="' + esc(prov)
                + '">copy file:line</button>' : "");
    openInspector(fromKeyboard);
  }
  info.addEventListener("click", function(ev){
    var b = ev.target.closest && ev.target.closest(".copy-prov");
    if(b && navigator.clipboard){ navigator.clipboard.writeText(b.getAttribute("data-prov")); return; }
    if(ev.target.closest && ev.target.closest(".rmore")){
      if(curNode){ showInfo(curNode, true); }
      return;
    }
    if(ev.target.closest && ev.target.closest(".tracebtn")){
      if(curNode){
        var n = traceDownstream(curNode);
        var h = document.querySelector("#info .tracebtn");
        if(h){ h.textContent = n + (n === 1 ? " node downstream" : " nodes downstream"); }
      }
      return;
    }
    var rn = ev.target.closest && ev.target.closest(".rn");
    if(rn){
      var node = cy.getElementById(rn.getAttribute("data-id"));
      if(node && node.nonempty()){ focus(node); showInfo(node); frameOn(node.closedNeighborhood()); }
    }
  });
  // Escape inside the inspector closes it and puts focus back where it came from, so a
  // keyboard user is never stranded in a panel they cannot leave in one step.
  info.addEventListener("keydown", function(e){
    if(e.key === "Escape"){ e.stopPropagation(); hideInfo(); restoreInvokerFocus(); }
  });
  function afterResize(){ cy.resize(); }  // ResizeObserver also catches the post-transition size
  // The inspector is a panel that appears AFTER the canvas in the DOM, so opening it
  // from the keyboard without moving focus would leave the user tabbing through the
  // whole graph region to reach the detail they just asked for (2.4.3). Mouse
  // activation still does not steal focus.
  var inspectInvoker = null;
  function openInspector(fromKeyboard){
    document.body.dataset.inspect = "open"; afterResize();
    if(fromKeyboard){
      inspectInvoker = document.activeElement;
      info.focus();
    }
  }
  function restoreInvokerFocus(){
    var el = inspectInvoker;
    inspectInvoker = null;
    if(el && el.isConnected && el.focus){ el.focus(); }
    else { var tv = document.getElementById("textview"); if(tv){ tv.querySelector("summary").focus(); } }
  }
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

  // Transitive downstream reach, as a SEPARATE action from focus(). focus() highlights the
  // closed neighbourhood in both directions, which is what you want while inspecting a
  // node, and it is shared with edge activation and the text view -- widening it would
  // change every one of those.
  //
  // `successors()` walks the graph CURRENTLY ON THE CANVAS, not the store. That is a real
  // limit, not an implementation detail: in --serve mode the canvas holds whatever has
  // been expanded so far, so the depth control decides how much of the true downstream
  // set is even present to be found. The button says so rather than letting a reader read
  // a partial answer as a complete one.
  function traceDownstream(node){
    var reach = node.successors();
    cy.elements().addClass("faded").removeClass("hi");
    reach.add(node).removeClass("faded").addClass("hi");
    refreshDomFx();
    marchAnts(reach.edges());
    return reach.nodes().length;
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
  // ONE activation path per element type, called by the mouse (cy "tap") and by the
  // text view's buttons alike. Keeping the text view on the same function is what makes
  // it a real equivalent: a keyboard user gets the namespace drill-in and, in --serve
  // mode, the /neighbors expansion, not a reduced imitation of them.
  function activateNode(n, fromKeyboard){
    // overview clusters mode: activating a namespace drills in/out (mindmap), not focus
    if(n.data("kind") === "namespace"){ toggleNs(n); return; }
    focus(n); showInfo(n, false, fromKeyboard);
    // overview repo nodes navigate via the inspector link, never /neighbors-expand
    if(LIVE && !OVERVIEW){ expand(n.id()); }
    else { frameOn(n.closedNeighborhood()); }
  }
  function activateEdge(ed, fromKeyboard){
    cy.elements().addClass("faded").removeClass("hi");
    ed.connectedNodes().add(ed).removeClass("faded").addClass("hi");
    refreshDomFx();
    marchAnts(ed);
    showEdgeInfo(ed, fromKeyboard);
    frameOn(ed.connectedNodes());
  }
  cy.on("tap", "node", function(e){ activateNode(e.target, false); });
  cy.on("tap", "edge", function(e){ activateEdge(e.target, false); });

  // ===== Text view: the graph, as text you can navigate ========================
  // A force-directed canvas has no accessible content: #cy's innerText is empty and
  // its entire accessible name was the string "Knowledge graph", so a screen-reader
  // user got a node/edge COUNT and nothing else — none of the structure the page
  // exists to communicate (1.1.1). Adding an aria-label to a <canvas> cannot fix that;
  // the only honest fix is a second, textual rendering of the same subgraph.
  //
  // It is deliberately the same data and the same handlers, not a description written
  // alongside them: it lists exactly the nodes that are VISIBLE (so in the overview's
  // collapsed clusters mode it lists the namespaces, and offers the same drill-in),
  // and activating an item calls activateNode/activateEdge — the very functions a
  // mouse tap calls. That is also what makes it the keyboard model (2.1.1): selecting
  // a node, opening the inspector, reading an edge's relation, confidence and
  // provenance, and expanding neighbours in --serve mode all become reachable.
  //
  // <details> closed by default keeps it out of the tab order until wanted and keeps
  // the sidebar's visual weight unchanged; the caps keep a 5000-node fleet from
  // materialising 40k buttons.
  var TV_MAX_NODES = 200, TV_MAX_EDGES = 8;
  var textDirty = true;
  var tvDetails = document.getElementById("textview");
  var tvBody = document.getElementById("tv-body");
  var tvNote = document.getElementById("tv-note");

  function tvKindColor(n){ return COLORS[n.data("kind")] || DEFAULT_COLOR; }
  function el(tag, cls, text){
    var e = document.createElement(tag);
    if(cls){ e.className = cls; }
    if(text != null){ e.textContent = text; }
    return e;
  }
  function tvNodeItem(n){
    var li = document.createElement("li");
    var b = el("button", "tv-n");
    b.type = "button";
    b.setAttribute("data-id", n.id());
    var dot = el("span", "tv-dot");
    dot.style.background = tvKindColor(n);
    b.appendChild(dot);
    b.appendChild(el("span", "tv-t", n.data("label") || n.id()));
    var isNs = n.data("kind") === "namespace";
    b.appendChild(el("span", "tv-k", isNs ? (nsExpanded[n.data("ns")] ? "collapse" : "expand")
                                          : (n.data("kind") || "node")));
    if(isNs){ b.setAttribute("aria-expanded", String(!!nsExpanded[n.data("ns")])); }
    li.appendChild(b);
    var es = n.connectedEdges().filter(function(e2){ return e2.visible(); });
    if(es.length){
      var ul = el("ul", "tv-conns");
      es.slice(0, TV_MAX_EDGES).forEach(function(ed){
        var out = ed.data("source") === n.id();
        var other = out ? ed.target() : ed.source();
        var eb = el("button", "tv-ed");
        eb.type = "button";
        eb.setAttribute("data-eid", ed.id());
        var bar = el("span", "tv-bar");
        bar.style.setProperty("--rel", REL_COLORS[ed.data("relation")] || DEFAULT_EDGE_COLOR);
        eb.appendChild(bar);
        // scaffold spokes carry no relation — they mean "is in this namespace"
        eb.appendChild(el("span", "tv-rel",
          (ed.data("relation") || (ed.data("scaffold") ? "in namespace" : "related"))
          + (out ? " →" : " ←")));
        eb.appendChild(el("span", "tv-nb", other.data("label") || other.id()));
        eb.appendChild(el("span", "tv-k", (ed.data("confidence") || "").toLowerCase()));
        var eli = document.createElement("li");
        eli.appendChild(eb);
        ul.appendChild(eli);
      });
      if(es.length > TV_MAX_EDGES){
        ul.appendChild(el("li", "tv-more", "+" + (es.length - TV_MAX_EDGES)
          + " more connections — open this node to see them all"));
      }
      li.appendChild(ul);
    }
    return li;
  }
  function renderTextView(){
    if(!tvDetails || !tvBody || !tvDetails.open || !textDirty) return;
    textDirty = false;
    // Expanding a namespace rebuilds this list, which would otherwise destroy the very
    // button the user just pressed and drop focus to <body> — so remember what was
    // focused and put focus back on the same element after the rebuild.
    var act = document.activeElement;
    var keep = (act && tvBody.contains(act))
      ? (act.getAttribute("data-id") ? '[data-id="' + CSS.escape(act.getAttribute("data-id")) + '"]'
                                     : null)
      : null;
    var vis = cy.nodes().filter(function(n){ return n.visible(); });
    var edgeCount = cy.edges().filter(function(e2){ return e2.visible(); }).length;
    tvBody.textContent = "";
    var list = el("ul", "tv-list");
    vis.slice(0, TV_MAX_NODES).forEach(function(n){ list.appendChild(tvNodeItem(n)); });
    tvBody.appendChild(list);
    if(keep){
      var again = tvBody.querySelector(".tv-n" + keep);
      if(again){ again.focus(); }
      else { tvDetails.querySelector("summary").focus(); }
    }
    // Same honesty rule the status bar follows: say when the list is capped rather
    // than letting it look complete.
    tvNote.textContent = vis.length + (vis.length === 1 ? " node" : " nodes") + " and "
      + edgeCount + (edgeCount === 1 ? " connection" : " connections") + " in view"
      + (vis.length > TV_MAX_NODES
          ? "; listing the first " + TV_MAX_NODES + " — use search or the filters to narrow"
          : "")
      + ". Activate a node to select it and open its details.";
  }
  function openTextView(focusFirst){
    if(!tvDetails) return;
    tvDetails.open = true;
    renderTextView();
    var first = tvBody.querySelector(".tv-n");
    (focusFirst && first ? first : tvDetails.querySelector("summary")).focus();
  }
  if(tvDetails){
    tvDetails.addEventListener("toggle", function(){ if(tvDetails.open){ renderTextView(); } });
    tvBody.addEventListener("click", function(ev){
      var nb = ev.target.closest && ev.target.closest(".tv-n");
      if(nb){
        var n = cy.getElementById(nb.getAttribute("data-id"));
        if(n && n.nonempty()){ activateNode(n, true); }
        return;
      }
      var eb = ev.target.closest && ev.target.closest(".tv-ed");
      if(eb){
        var ed = cy.getElementById(eb.getAttribute("data-eid"));
        if(ed && ed.nonempty()){ activateEdge(ed, true); }
      }
    });
    // 1.4.13's other half: the hover tooltip's content (kind for a node; relation,
    // confidence and provenance for an edge) is shown on FOCUS too, anchored to the
    // focused item, so it is not mouse-only. Escape dismisses it; the shared handler
    // above does that.
    tvBody.addEventListener("focusin", function(ev){
      var t = ev.target;
      var box = t.getBoundingClientRect ? t.getBoundingClientRect() : null;
      var txt = "";
      if(t.classList && t.classList.contains("tv-n")){
        var n = cy.getElementById(t.getAttribute("data-id"));
        if(n && n.nonempty()){ txt = nodeTipText(n); }
      } else if(t.classList && t.classList.contains("tv-ed")){
        var ed2 = cy.getElementById(t.getAttribute("data-eid"));
        if(ed2 && ed2.nonempty()){ txt = edgeTipText(ed2); }
      }
      if(txt && box){
        tipText(txt);
        showTipAt(box.left + window.scrollX + 8, box.bottom + window.scrollY + 4);
      }
    });
    tvBody.addEventListener("focusout", function(){ hideTip(); });
  }
  // Anything that changes what is on screen invalidates the list. cy fires clake-vis
  // for the filter/overview paths, and layoutstop/add/remove cover layout changes and
  // the LIVE neighbour expansion.
  cy.on("clake-vis layoutstop add remove", function(){
    textDirty = true; renderTextView();
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
        octx.fillStyle = ns ? (EDGE_INK[themeName()] || EDGE_INK.light).ns
                            : (COLORS[n.data("kind")] || DEFAULT_COLOR);
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
    // Was a wrapper around the theme BUTTON's onclick, which missed the OS-preference,
    // ?theme= and postMessage paths entirely — the viewport rectangle kept the old
    // theme's brand colour whenever the dashboard drove the theme. Ride applyTheme.
    onTheme(function(){ drawStatic(); });
    drawStatic();
  })();

  // LAST, after every onTheme() hook is registered. Two of the four theme entry
  // points (the OS preference and ?theme= on the src) fire while this file is still
  // being evaluated, i.e. before the legend-repaint and minimap hooks exist -- so on
  // an OS-dark or ?theme=dark FIRST PAINT the canvas used the dark relation palette
  // while the legend that documents it kept the server-rendered light hues. That is
  // not just an inconsistency: several light hues fall under 3:1 on the dark surface,
  // which is the very failure the per-theme palette exists to fix. One forced re-apply
  // settles every hook at whatever theme we ended up in.
  applyTheme(themeName(), true);

  // The depth slider and the direction select both feed /neighbors, which runs the
  // traversal server-side. Narrowing a both-direction fetch down to a directed view in
  // the browser was measured against the live store and loses up to 11 of 14 nodes:
  // max_fanout is applied to the combined neighbour list, so in-edges crowd out the
  // out-edges the directed view needs and no later hop brings them back. Depth could
  // safely be narrowed client-side (measured: never loses a node) but there is nothing
  // to gain -- these controls change the NEXT expand rather than repainting the canvas,
  // so the slider costs no round-trip of its own either way.
  var hopsEl = document.getElementById("hops");
  var dirEl = document.getElementById("direction");
  var hopsOut = document.getElementById("hopsv");
  if(hopsEl && hopsOut){
    hopsEl.addEventListener("input", function(){ hopsOut.textContent = hopsEl.value; });
  }
  function expandHops(){ return hopsEl ? hopsEl.value : "1"; }
  function expandDir(){ return dirEl ? dirEl.value : "both"; }

  function expand(id){
    var cyEl = document.getElementById("cy");
    cyEl.classList.add("loading");
    fetch("/neighbors?id=" + encodeURIComponent(id)
          + "&hops=" + encodeURIComponent(expandHops())
          + "&direction=" + encodeURIComponent(expandDir()))
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
