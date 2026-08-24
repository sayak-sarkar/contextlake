/* Try-it query box for the docs site.
 *
 * Runs entirely in the browser against the demo fleet's symbol table (demo/data.json), which the
 * dashboard export already ships. No backend, no network beyond that one fetch, nothing to keep
 * running: the same local-first stance the product takes.
 *
 * Deliberately NOT a semantic search. The real thing needs an embedding model; claiming semantic
 * behaviour from a substring match here would misrepresent the product on its own docs page.
 * This shows the SHAPE of a result -- repo, file, kind, cited name -- which is what a reader is
 * actually trying to understand before they install anything.
 */
(function () {
  var box = document.getElementById('tryit');
  if (!box) return;
  var input = box.querySelector('input');
  var out = box.querySelector('.tryit-out');
  var symbols = null, loading = false;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function score(sym, q) {
    var hay = (sym.name + ' ' + sym.repo + ' ' + sym.file + ' ' + sym.kind).toLowerCase();
    if (sym.name.toLowerCase() === q) return 100;
    if (sym.name.toLowerCase().indexOf(q) === 0) return 60;
    if (hay.indexOf(q) !== -1) return 20;
    return 0;
  }

  function render(q) {
    if (!symbols) { out.innerHTML = '<p class="tryit-note">loading the demo fleet…</p>'; return; }
    q = q.trim().toLowerCase();
    if (!q) { out.innerHTML = '<p class="tryit-note">Type a symbol, a repo, or a file name.</p>'; return; }
    var hits = symbols.map(function (s) { return { s: s, n: score(s, q) }; })
                      .filter(function (h) { return h.n > 0; })
                      .sort(function (a, b) { return b.n - a.n; })
                      .slice(0, 8);
    if (!hits.length) {
      out.innerHTML = '<p class="tryit-note">No match in the demo fleet. ' +
        'It holds ' + symbols.length + ' symbols across a handful of sample repos, ' +
        'so a miss here says nothing about your own code.</p>';
      return;
    }
    out.innerHTML = '<ol class="tryit-hits">' + hits.map(function (h) {
      var s = h.s;
      // Join only the parts that exist: a package symbol has no file, and a dangling
      // separator reads as a rendering bug rather than as absent data.
      var cite = [s.repo, s.file, s.lang].filter(Boolean).map(esc).join(' · ');
      return '<li><code>' + esc(s.name) + '</code> <span class="tryit-kind">' + esc(s.kind) +
             '</span><br><span class="tryit-cite">' + cite + '</span></li>';
    }).join('') + '</ol><p class="tryit-note">Every hit names its repo and file. That citation is ' +
      'the point: an answer you can open.</p>';
  }

  function load() {
    if (loading || symbols) return;
    loading = true;
    fetch('demo/data.json').then(function (r) { return r.json(); }).then(function (d) {
      symbols = (d && d.symbols) || [];
      render(input.value);
    }).catch(function () {
      out.innerHTML = '<p class="tryit-note">Could not load the demo data. ' +
        'The <a href="demo/index.html">full demo dashboard</a> has the same fleet.</p>';
    });
  }

  input.addEventListener('focus', load);
  input.addEventListener('input', function () { load(); render(input.value); });
  render('');
})();
