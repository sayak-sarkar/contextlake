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
      { selector: "node.hi", style: { "border-width": 3, "border-color": "#2BB3A3",
          "z-index": 99 } },
      { selector: "node.found", style: { "border-width": 4, "border-color": "#E7B53C",
          "z-index": 100 } },
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
    style: graphStyle(),
    layout: { name: "preset" }
  });

  // Keep the cytoscape <canvas> synced to its grid cell through ANY layout change
  // (inspector slide-in, sidebar collapse, window resize) — robust, no timing
  // guess. cy.resize() re-reads the container each frame the cell animates.
  if(window.ResizeObserver){ new ResizeObserver(function(){ cy.resize(); }).observe(cyEl); }

  cy.nodes().forEach(function(n){ n.data("deg", n.degree(false)); });
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
  document.getElementById("navToggle").onclick = function(){
    var c = document.body.dataset.sidebar === "collapsed";
    document.body.dataset.sidebar = c ? "open" : "collapsed"; afterResize();
  };
  // "Fit" frames the readable view: the connected core when isolated repos
  // dominate (the fleet overview), else the whole graph.
  function reframe(){
    var core = cy.nodes().filter(function(n){ return n.degree(false) > 0; });
    var dominated = core.nonempty() && (cy.nodes().length - core.length) > core.length;
    cy.fit(dominated ? core : undefined, 30);
  }
  document.addEventListener("keydown", function(e){
    if(e.target.tagName === "INPUT"){ if(e.key === "Escape"){ e.target.blur(); } return; }
    if(e.key === "/"){ e.preventDefault(); document.getElementById("search").focus(); }
    else if(e.key === "f" || e.key === "F"){ reframe(); }
    else if(e.key === "t" || e.key === "T"){ document.getElementById("theme").click(); }
    else if(e.key === "Escape"){ cy.elements().removeClass("faded hi"); hideInfo(); }
  });

  function layoutOpts(name){
    if(name === "cose") return { name:"cose", animate:false, randomize:true, padding:40,
        nodeOverlap:24, componentSpacing:140, gravity:0.2, numIter:1500,
        nodeRepulsion:function(){ return 14000; },
        idealEdgeLength:function(){ return 120; }, edgeElasticity:function(){ return 80; } };
    if(name === "concentric") return { name:"concentric", animate:false, padding:40,
        minNodeSpacing:28, concentric:function(n){ return n.degree(false); },
        levelWidth:function(){ return 2; } };
    if(name === "breadthfirst") return { name:"breadthfirst", animate:false, padding:40,
        spacingFactor:1.5, circle:false };
    if(name === "circle") return { name:"circle", animate:false, padding:40, spacingFactor:1.3 };
    if(name === "grid") return { name:"grid", animate:false, padding:40, avoidOverlap:true,
        avoidOverlapPadding:24 };
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
    cy.fit(undefined, 30);
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
  }
  function layoutClusters(){
    var vis = cy.elements().filter(function(el){ return el.visible(); });
    vis.layout({ name: "cose", animate: false, randomize: true, padding: 40,
      nodeOverlap: 24, componentSpacing: 120, gravity: 0.3, numIter: 1200,
      nodeRepulsion: function(){ return 12000; },
      idealEdgeLength: function(e){ return e.data("scaffold") ? 64 : 210; } }).run();
    cy.fit(vis, 45);
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
    cy.fit(core, 45);
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
  // no disorienting global reshuffle. Collapse just hides them.
  function toggleNs(nsNode){
    var ns = nsNode.data("ns");
    var opening = !nsExpanded[ns];
    nsExpanded[ns] = opening;
    cy.elements().removeClass("faded hi found");
    applyOverview();
    if(opening){
      var kids = cy.nodes('[kind = "repo"]').filter(function(n){ return n.data("ns") === ns; });
      // compact grid directly beneath the namespace node (a mindmap branch), so it
      // stays tight instead of a wide ring that collides with other namespaces
      var cx = nsNode.position("x"), cy0 = nsNode.position("y");
      var cols = Math.max(1, Math.ceil(Math.sqrt(kids.length))), sp = 72;
      var w = cols * sp, h = Math.ceil(kids.length / cols) * sp;
      kids.layout({ name: "grid", animate: false, avoidOverlap: true, condense: true,
        boundingBox: { x1: cx - w / 2, y1: cy0 + 56, w: w, h: h } }).run();
      var grp = nsNode.union(kids);
      cy.elements().addClass("faded");        // spotlight the opened branch
      grp.union(kids.connectedEdges()).removeClass("faded");
      cy.animate({ fit: { eles: grp, padding: 55 } }, { duration: dur(350) });
    } else {
      cy.animate({ fit: { eles: cy.nodes('[kind = "namespace"]').filter(function(n){
        return n.visible(); }), padding: 45 } }, { duration: dur(350) });
    }
  }

  if(OVERVIEW){
    buildOverviewModel();
    document.getElementById("viewmodes").hidden = false;
    document.getElementById("vm-clusters").onclick = function(){ setMode("clusters"); };
    document.getElementById("vm-flow").onclick = function(){ setMode("flow"); };
    setMode("clusters");
  } else {
    runLayout(LAYOUT);
  }

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
    if(OVERVIEW){ relayoutOverview(); } else { runLayout(sel.value); }
  });

  // toolbar
  document.getElementById("fit").onclick = function(){ reframe(); };
  document.getElementById("png").onclick = function(){
    var uri = cy.png({ full:true, scale:2, bg:"#ffffff" });
    var a = document.createElement("a");
    a.href = uri; a.download = "contextlake-graph.png"; a.click();
  };
  document.getElementById("reset").onclick = function(){
    cy.elements().removeClass("faded hi found");
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
  function restoreVisibility(){ if(OVERVIEW){ applyOverview(); } else { applyFilter(); } }
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
    if(hits.length){ cy.animate({ fit:{ eles:hits, padding:90 } }, { duration: dur(300) }); }
  });

  // hover tooltip
  var tip = document.getElementById("tip");
  cy.on("mouseover", "node", function(e){
    var n = e.target;
    tip.textContent = (n.data("label")||"") + "  \u00b7  " + (n.data("kind")||"");
    tip.style.display = "block";
  });
  cy.on("mousemove", function(e){
    if(tip.style.display === "block"){
      tip.style.left = (e.renderedPosition.x + 12) + "px";
      tip.style.top  = (e.renderedPosition.y + 12) + "px";
    }
  });
  cy.on("mouseout", "node", function(){ tip.style.display = "none"; });
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
      cy.animate({ fit:{ eles: eles, padding: 80 } }, { duration: dur(300) });
    }, 210);
  }

  function focus(node){
    cy.elements().addClass("faded").removeClass("hi");
    node.closedNeighborhood().removeClass("faded").addClass("hi");
  }
  cy.on("tap", function(e){
    if(e.target === cy){ cy.elements().removeClass("faded hi"); hideInfo(); }
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
    showEdgeInfo(ed);
    frameOn(ed.connectedNodes());
  });

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
