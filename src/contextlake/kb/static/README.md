# Vendored static assets

These files are bundled so `contextlake graph --format html` renders **offline**
(air-gapped / behind a TLS-inspecting proxy) with no CDN dependency.

## cytoscape.min.js

- **Library:** [cytoscape.js](https://js.cytoscape.org/)
- **Version:** 3.34.0
- **Source:** `https://cdn.jsdelivr.net/npm/cytoscape@3.34.0/dist/cytoscape.min.js`
- **License:** MIT — © The Cytoscape Consortium. Compatible with contextlake's MIT license.

`kb/visualize/html_render.py::to_html` inlines this file into the generated HTML by
default; pass `--cdn` to reference the CDN copy instead (smaller file, requires network).

To update: download the pinned version from jsdelivr, replace the file, bump the
version here and the `_CDN_URL` constant in `kb/visualize/html_render.py`.

(`mermaid.min.js`, used by the dashboard's Diagrams tab, lives in
`kb/dashboard/static/` alongside the SPA shell it's lazy-loaded into — see that
directory's own README, not here.)
