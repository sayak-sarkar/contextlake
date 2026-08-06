### Fixed
- **SECURITY: `[[sources]]` ingest could be pointed at a local file instead of a URL.** The
  `web`, `api` and `graphql` sources passed their configured `url` straight to `urllib`, which
  speaks `file:`, `ftp:` and `data:` as readily as `https:`. A `url = "file:///…"` therefore
  read that file off disk and ingested its contents as a document -- afterwards visible in the
  graph, the wiki, the dashboard and every connected MCP client.

  This mattered because a source URL is not necessarily one you chose. contextlake discovers
  `.contextlake.kb.toml` by walking up from the current directory, and it clones repositories
  into your workspace itself, so a checkout could supply the config that supplies the URL --
  no action needed beyond working in that directory. The existing workspace-trust gate covers
  config keys that reach a subprocess and deliberately leaves `url` alone as "an HTTP
  endpoint"; that is now true rather than assumed.

  Ingest fetchers now open `http` and `https` only, and log a warning naming the refused
  scheme rather than skipping quietly. Configured `http(s)` sources are unaffected. Requests
  to private or link-local addresses are still permitted -- `SECURITY.md` now says so
  explicitly. If you need the discovered-config tier gone entirely,
  `CONTEXTLAKE_NO_LOCAL_CONFIG=1` still does that.
