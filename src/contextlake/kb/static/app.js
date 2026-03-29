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

  var cy = cytoscape({
    container: document.getElementById("cy"),
    elements: ELEMENTS,
    wheelSensitivity: 0.2,
    style: graphStyle(),
    layout: { name: "preset" }
  });

  cy.nodes().forEach(function(n){ n.data("deg", n.degree(false)); });
  document.getElementById("mode").textContent = META.mode || "graph";
  document.getElementById("meta").textContent =
    cy.nodes().length + " nodes \u00b7 " + cy.edges().length + " edges";
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
  document.addEventListener("keydown", function(e){
    if(e.target.tagName === "INPUT"){ if(e.key === "Escape"){ e.target.blur(); } return; }
    if(e.key === "/"){ e.preventDefault(); document.getElementById("search").focus(); }
    else if(e.key === "f" || e.key === "F"){ cy.fit(undefined, 30); }
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
  function runLayout(name){ cy.layout(layoutOpts(name)).run(); cy.fit(undefined, 30); }
  runLayout(LAYOUT);

  var sel = document.getElementById("layout");
  sel.value = LAYOUT;
  sel.addEventListener("change", function(){ runLayout(sel.value); });

  // toolbar
  document.getElementById("fit").onclick = function(){ cy.fit(undefined, 30); };
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
  function showInfo(n){
    var d = n.data();
    var fileline = d.file ? (d.file + (d.line ? ":" + d.line : "")) : "";
    info.innerHTML = "<h2>" + esc(d.label || d.id) + "</h2><dl>"
      + row("kind", d.kind) + row("repo", d.repo) + row("qualified", d.qn)
      + row("file", fileline) + row("nodes", d.count) + row("degree", d.deg) + "</dl>"
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
    if(b && navigator.clipboard){ navigator.clipboard.writeText(b.getAttribute("data-prov")); }
  });
  function afterResize(){ setTimeout(function(){ cy.resize(); }, 190); }
  function openInspector(){ document.body.dataset.inspect = "open"; afterResize(); }
  function hideInfo(){ document.body.dataset.inspect = "closed"; afterResize(); }

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
  });
  cy.on("tap", "edge", function(e){
    var ed = e.target;
    cy.elements().addClass("faded").removeClass("hi");
    ed.connectedNodes().add(ed).removeClass("faded").addClass("hi");
    showEdgeInfo(ed);
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
