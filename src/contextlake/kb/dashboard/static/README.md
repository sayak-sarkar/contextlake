# Dashboard SPA assets

`dashboard.html`/`dashboard.js`/`dashboard.css` are the `contextlake dashboard --serve`
single-page app shell, served via `importlib.resources` (see `kb/dashboard/server.py`).
`mermaid.min.js` is a vendored third-party library alongside them.

## mermaid.min.js

- **Library:** [Mermaid](https://mermaid.js.org/)
- **Version:** 11.16.0
- **Source:** `https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.min.js`
- **License:** MIT -- © Knut Sveidqvist and Mermaid contributors. Compatible with
  contextlake's MIT license.

Used by the repo page's **Diagrams** tab to render Mermaid text
(`classdiagram`/`statediagram`/`erdiagram`/`deploymentdiagram`/generic `mermaid`,
the same text `contextlake graph --format ...` produces) as an inline SVG in the
browser. At ~3.5MB this is much larger than `kb/static/cytoscape.min.js`, so
`dashboard.js`'s `loadMermaid()` injects a `<script src="mermaid.min.js">` tag
only the first time the Diagrams tab is opened, not in the base page shell -- 
served offline from this same directory, no CDN dependency.

Initialized with `securityLevel: "strict"` (mermaid's own DOMPurify-sanitized
mode): diagram text embeds repo-derived symbol/table/resource names, which are
untrusted input.

To update: download the pinned version from jsdelivr, replace the file, bump
the version here and in `dashboard.js`'s comments referencing it.

## See also

- [Using the dashboard](../../../../docs/using-the-dashboard.md)
- [The graph view's static assets](../../static/README.md)
