"""contextlake knowledge layer (preview).

An optional subsystem that indexes the mirrored repositories into a queryable
knowledge graph and serves it to AI agents over MCP. Generic by design: it
indexes *any* git repositories and connects to *any* configured knowledge
sources — no organization-specific data lives in this package (see the project
docs, principle G1).

Install with the ``kb`` extra::

    pip install "contextlake[kb]"

Requires Python >= 3.10 (the ``mcp`` dependency's floor); the core sync tool
still supports Python >= 3.9.
"""
