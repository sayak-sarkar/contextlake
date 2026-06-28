"""The knowledge-system dashboard — the primary human UI into the local graph.

Three layers, all reuse-first and offline-first:

* ``data`` — pure, JSON-able functions over a ``Store`` (no I/O beyond the store and
  the local mirror it points at). Each reuses the function behind an existing MCP
  tool, so the dashboard and the MCP surface never drift.
* ``server`` — a stdlib ``ThreadingHTTPServer`` that serves a small JSON API plus the
  SPA shell and proxies the existing cytoscape graph pages, opening one short-lived
  ``Store`` per request for SQLite thread-affinity.
* ``site`` — a static, offline ``--site`` export (SPA shell + one ``data.json``
  snapshot + the iframed graph site), with a binding PII guardrail.
"""
