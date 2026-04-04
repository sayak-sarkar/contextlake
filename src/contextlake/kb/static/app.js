function edgeColor(e){ return REL_COLORS[e.data("relation")] || DEFAULT_EDGE_COLOR; }
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
          "label": "data(label)", "font-size": 9, "color": label,
          "width": "mapData(deg, 0, 24, 14, 52)", "height": "mapData(deg, 0, 24, 14, 52)",
          "text-wrap": "ellipsis", "text-max-width": 120,
          "text-valign": "bottom", "text-margin-y": 2,
          "border-width": 0.5, "border-color": surf } },
      { selector: "edge", style: {
          "line-color": edgeColor, "target-arrow-color": edgeColor,
          "width": "mapData(weight, 1, 10, 0.8, 4.5)",
          "target-arrow-shape": "triangle", "arrow-scale": 0.7, "curve-style": "bezier" } },
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
          "text-background-opacity": 0.9, "z-index": 99 } }
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
  var isolated = cy.nodes().filter(function(n){ return n.data("deg") === 0; }).length;
  document.getElementById("meta").textContent =
    cy.nodes().length + " nodes \u00b7 " + cy.edges().length + " edges"
    + (isolated ? " \u00b7 " + isolated + " isolated" : "");
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
  // The fleet overview is mostly isolated repos (no detected deps). Mixed into one
  // layout they scatter the connected dependency map across the whole canvas and
  // crush it into an unreadable speck. When they dominate, lay out ONLY the
  // connected core compactly and park the isolated repos in a tidy grid block
  // beneath it (still present + searchable), then frame the readable core.
  function runLayout(name){
    var core = cy.nodes().filter(function(n){ return n.degree(false) > 0; });
    var iso = cy.nodes().not(core);
    if(core.nonempty() && iso.length > core.length){
      core.layout(layoutOpts(name)).run();
      var bb = core.boundingBox();
      var cols = Math.max(1, Math.ceil(Math.sqrt(iso.length)));
      var gap = 64, x0 = bb.x1, y0 = bb.y2 + 160;
      iso.forEach(function(n, i){
        n.position({ x: x0 + (i % cols) * gap, y: y0 + Math.floor(i / cols) * gap });
      });
      cy.fit(core, 40);
    } else {
      cy.layout(layoutOpts(name)).run();
      cy.fit(undefined, 30);
    }
  }
  runLayout(LAYOUT);

  var sel = document.getElementById("layout");
  sel.value = LAYOUT;
  sel.addEventListener("change", function(){ runLayout(sel.value); });

  // toolbar
  document.getElementById("fit").onclick = function(){ reframe(); };
  document.getElementById("png").onclick = function(){
    var uri = cy.png({ full:true, scale:2, bg:"#ffffff" });
    var a = document.createElement("a");
    a.href = uri; a.download = "contextlake-graph.png"; a.click();
  };
  document.getElementById("reset").onclick = function(){
    cy.elements().removeClass("faded hi found");
    hidden = {}; hiddenRel = {}; applyFilter(); syncLegend();
    document.getElementById("search").value = "";
    hideInfo(); cy.fit(undefined, 30);
  };

  // legends = kind filter (nodes) + relationship filter (edges)
  var hidden = {}, hiddenRel = {};
  function applyFilter(){
    cy.nodes().forEach(function(n){
      n.style("display", hidden[n.data("kind")] ? "none" : "element");
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

  // search -> highlight + frame matches
  var search = document.getElementById("search");
  search.addEventListener("input", function(){
    var q = search.value.trim().toLowerCase();
    cy.nodes().removeClass("found");
    if(!q) return;
    var hits = cy.nodes().filter(function(n){
      return (n.data("label")||"").toLowerCase().indexOf(q) >= 0
          || (n.data("qn")||"").toLowerCase().indexOf(q) >= 0;
    });
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
    var prov = d.prov_file
      ? "  \u00b7  " + d.prov_file + (d.prov_line ? ":" + d.prov_line : "") : "";
    tip.textContent = d.relation + "  \u00b7  " + d.confidence + prov;
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
    focus(e.target); showInfo(e.target);
    if(LIVE){ expand(e.target.id()); }
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
