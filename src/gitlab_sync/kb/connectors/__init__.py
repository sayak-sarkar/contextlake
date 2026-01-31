"""Knowledge-source connectors.

Pluggable connectors that enrich the code graph with external knowledge
(Atlassian issues/pages, Figma designs, …) fetched over MCP and linked to repos
by the association signals in ``kb.references``. Each connector is generic; the
sites/credentials/rules come from user config.
"""
