# Vendored static assets

These files are bundled so `contextlake graph --format html` renders **offline**
(air-gapped / behind a TLS-inspecting proxy) with no CDN dependency.

## cytoscape.min.js

- **Library:** [cytoscape.js](https://js.cytoscape.org/)
- **Version:** 3.30.2
- **Source:** `https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js`
- **License:** MIT — © The Cytoscape Consortium. Compatible with contextlake's MIT license.

`kb/visualize.py::to_html` inlines this file into the generated HTML by default;
pass `--cdn` to reference the CDN copy instead (smaller file, requires network).

To update: download the pinned version from jsdelivr, replace the file, bump the
version here and the `_CDN_URL` constant in `kb/visualize.py`.
