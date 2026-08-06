### Added
- **Dashboard: the fleet-wide Architecture "Overview" graph now has a real text/table
  equivalent (WCAG 1.1.1 Non-text Content).** A single repo's Architecture view already
  had one -- a genuine tabbed table of Dependencies/HTTP flow/Event flow, not a token
  gesture -- but picking no repo (the Overview scope, showing every repo and their
  cross-repo edges at once) rendered only an invitation to go pick one, with no
  equivalent for the fleet-wide picture itself. A screen-reader user could reconstruct
  it by visiting each repo's own tab in turn, but never got the sighted user's
  one-screen overview. The same three edge categories are now available unfiltered by
  repo -- sourced from the same underlying edge scan the graph itself uses, capped at
  500 rows per category with a banner if truncated -- reachable the same way the
  per-repo tables are (a "Skip past graph" link, then a tabbed, `columnheader`/
  `rowheader`-marked table with a working provenance button per row). A static `--site`
  export built before this shipped simply has no data for this table and falls back to
  the original "pick a repo" invitation rather than an error.
