### Fixed
- **SECURITY: generated graph pages could execute HTML/JavaScript taken from an indexed
  repository (stored cross-site scripting).** The graph payload was embedded in the page's
  inline `<script>` with no escaping, so a `</script>` sequence anywhere in indexed content
  closed the element early and the browser parsed the rest of the payload as markup. The
  reachable inputs are ordinary repository data -- a symbol name, a file name, commit
  context, or a connector/web-page title -- so anyone able to land a string in a repository
  you index could choose what ran in your browser when you opened the page. It mattered most
  on `kb dashboard --serve`, which serves those pages on the same origin as the script
  carrying the per-process mutation/LLM token: injected code there could read the token and
  drive the mutation and chat endpoints. `kb graph`'s standalone HTML file, `kb graph --c4`,
  `kb graph --serve`, and the `build_site` page set were all affected, since all four render
  through the same function.

  Every payload entering a script context now goes through one shared escape
  (`kb.security.json_for_script`), which the static `--site` export already applied to its
  own snapshot and now shares rather than duplicating. Repository text is additionally
  escaped where it reaches an HTML attribute or element text: the kind and relationship
  legends, the page title, the wiki staleness badge, and the site index's repo links and
  headings. Hostile values are rendered verbatim as inert text, so graphs look exactly as
  before -- output for ordinary content is byte-for-byte unchanged.

  Page templates are also filled in a single pass now. Previously each placeholder was
  substituted in turn over the whole document, so repository text that merely *spelled* a
  later placeholder (for example a symbol named `__GLYPH__`) had template markup inserted
  into the middle of the data after escaping had run -- corrupting the page in a way no
  amount of character escaping could prevent.

  **What to do:** upgrade -- no configuration change is needed. Then **regenerate any graph
  HTML you saved, shared or published**: files written by an earlier version are static
  artifacts that still carry the unescaped payload, and upgrading cannot retroactively fix a
  file already on disk. That means anything produced by `kb graph -o …`, `kb graph --c4` or
  `kb dashboard --site`. It matters most for a file you sent to someone else or put on a web
  server, and least for one you generated from code you wrote yourself. Pages served live by
  `kb dashboard --serve` and `kb graph --serve` are rendered per request, so they are fixed by
  the upgrade alone.
