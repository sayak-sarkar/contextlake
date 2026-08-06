### Fixed
- **`kb index`'s "Workspace indexed" summary reports the workspace, not the whole store.** It printed
  `store.stats()` -- a store-wide count over every repo the store has ever indexed, from any
  `--workspace` -- under a line labelled with this run's workspace. On a real fleet the line read
  "Workspace indexed: 21 repos" two lines after "Found 19 repositories under repositories", and the
  store itself held 39 distinct repo ids: three disagreeing denominators for what should have been
  one number. The summary now sums `repo_counts()` over exactly the repo list this run discovered
  under the named workspace -- the same list the "Found N repositories" line above it counts -- so
  the two lines can never disagree, and an unrelated repo indexed by an earlier run under a different
  workspace can no longer inflate this one's numbers.
