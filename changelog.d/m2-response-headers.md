### Added
- **Security response headers on every local HTTP server.** `kb dashboard --serve`,
  `kb graph --serve` and the served static site now send a `Content-Security-Policy`,
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on every response.
  The policy is stated once in the shared server base, so all three servers get it and a
  future server inherits it.

  This is defence in depth, not a fix on its own: the policy's job is to contain a page
  that has already gone wrong. `default-src 'none'` with `connect-src 'self'` means a
  script running on the dashboard's origin cannot send anything to another host -- no
  `fetch`, no beacon, no form post -- which is the step that would turn a page-render into
  data leaving your machine. `frame-src`/`frame-ancestors` stay `'self'` so the dashboard's
  architecture panel keeps working, `img-src` allows the `data:` URIs the node glyphs use,
  and the jsDelivr origin is permitted for scripts because `kb graph --serve --cdn` loads
  cytoscape from there. Inline scripts and styles are allowed, since the pages inline their
  own assets by design.
