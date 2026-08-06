### Added
- **`--repos-exact` for an exact repo id/path match.** `--repos` has always matched a plain
  pattern as a substring of a repo's id or local path (documented, but easy to be surprised
  by): on a real fleet, `--repos atlas` selected the intended repo plus an unrelated one whose
  name merely contained "atlas". `--repos-exact` drops that substring leg while keeping glob
  patterns (`frontend/*`) working exactly as before, for anyone who wants `--repos` to mean
  "this repo, not also anything that happens to contain its name." The default is unchanged --
  `--repos` alone still matches on substring, so nobody's existing script silently starts
  matching less.
