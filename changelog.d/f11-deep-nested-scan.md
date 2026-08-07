### Fixed
- **`kb index` now sees nested repositories at any depth, not just direct children.** The
  bundling check asked `src.glob("*/.git")`, which matches one level down, so a fleet mirrored
  under a subdirectory was invisible to it: on a real workspace it reported "contains 1" where
  the truth was 20. That count is the whole point of the message -- "1" reads as an edge case
  worth skipping past, "20" is a stop sign -- so undercounting by 95% muted the warning at
  exactly the moment it needed to be loudest. The scan now shares `iter_repo_dirs` with
  `discover_repos`, so the number it reports and the set `--workspace` would actually walk
  cannot drift apart, and it names how deep the repositories sit.
