# Vendored static assets

These files are bundled so `contextlake graph --format html` renders **offline**
(air-gapped / behind a TLS-inspecting proxy) with no CDN dependency.

| File | Library | Version | License |
| --- | --- | --- | --- |
| `cytoscape.min.js` | [cytoscape.js](https://js.cytoscape.org/) | 3.34.0 | MIT — © The Cytoscape Consortium |
| `cytoscape-dagre.min.js` | [cytoscape.js-dagre](https://github.com/cytoscape/cytoscape.js-dagre) | 4.0.0 | MIT |
| `cytoscape-dom-node.js` | [cytoscape-dom-node](https://www.npmjs.com/package/cytoscape-dom-node) | 2.1.0 | MIT |

All three are MIT, compatible with contextlake's own MIT license.

Sources (jsDelivr, pinned):

- `https://cdn.jsdelivr.net/npm/cytoscape@3.34.0/dist/cytoscape.min.js`
- `https://cdn.jsdelivr.net/npm/cytoscape-dagre@4.0.0/dist/cytoscape-dagre.min.js`
- `https://cdn.jsdelivr.net/npm/cytoscape-dom-node@2.1.0/dist/index.global.js`

`kb/visualize/html_render.py::to_html` inlines these files into the generated HTML by
default; pass `--cdn` to reference the CDN copies instead (smaller file, requires
network). `build_site` writes them once as shared siblings.

The only edit made to the upstream files is stripping the trailing
`//# sourceMappingURL=` comment from the two extension files — the `.map` files are
not vendored, so the reference would 404 in devtools and quietly break the
"no network" promise. Upstream sha256 of the files as downloaded, before that strip:

- `cytoscape-dagre.min.js` — `b9e9d704119970f4255c035baa98d778e94af4b2efd2bdba20a601a869417223`
- `cytoscape-dom-node.js` — `1eb8a9ec88dbd1ddc4b63953bd1fe1c40ea676e68fbbfd81bd57a3a3700f0e28`

## Why the two extensions

They back the **opt-in `dagre (preview)` layout** in the graph page's layout
dropdown, and nothing else — every other layout renders exactly as it did before
they were vendored:

- **cytoscape-dagre** — directed, layered (rank) layout. It bundles dagre itself, so
  there is no separate `dagre.js` to vendor. UMD; exposes `cytoscapeDagre` and is
  registered by `app.js` via `cytoscape.use(...)`.
- **cytoscape-dom-node** — renders nodes as real HTML elements (border-radius,
  shadow, real typography) instead of canvas circles. The browser-global build
  self-registers against `window.cytoscape`, so it must load *after* `cytoscape.min.js`.

`app.js` feature-detects both: if either script did not load, the preview option
removes itself from the dropdown rather than offering a mode that would do nothing.

> **Known gap:** `kb/visualize/serve.py`'s lazy `--serve` site serves a hard-coded
> asset list (`app.css` / `app.js` / `cytoscape.min.js`) and does not yet serve these
> two files, so the preview option is absent there. `--format html` (inlined) and
> `--site` (sibling files) both have it.

To update: download the pinned version from jsDelivr, replace the file, then bump the
version **here** *and* the matching `_CDN_URL` / `_EXT_CDN_URLS` constants in
`kb/visualize/html_render.py`.

(`mermaid.min.js`, used by the dashboard's Diagrams tab, lives in
`kb/dashboard/static/` alongside the SPA shell it's lazy-loaded into — see that
directory's own README, not here.)
