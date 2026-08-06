### Fixed
- **SECURITY: the dashboard's wiki route could be steered to read Markdown files outside
  the store on Windows (path traversal).** The `?module=` value and the repo id in the URL
  are both turned into a wiki filename, and only `/` was being replaced -- so a
  `\`-separated value walked out of the wiki directory and the file's contents came back
  rendered. It affected Windows hosts, including the shipped `contextlake-windows-x86_64.exe`;
  POSIX happened to be unaffected because `\` is an ordinary filename character there. Reading
  was limited to files whose name ends `.md`, and required access to the dashboard, which binds
  to loopback by default.

  Wiki filenames are now built with a character allowlist that folds every path separator,
  and -- independently of that -- the read path verifies the resolved file really sits inside
  the store's wiki directory before opening it, so the containment holds even if a future
  change to the naming rules reintroduces a separator. A blocked request reads as "no such
  page" rather than an error. Legitimate module page names are unchanged, including
  non-ASCII directory names, so no already-generated page is orphaned.
