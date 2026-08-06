### Added
- **The dependency-vulnerability scan now covers the dependencies that actually ship.** The
  CI audit installed every extra except `llm-local`, because that one compiles from source and
  made the job flaky. The gap was larger than it looked: `llm-local` is what the published
  `full`/`latest` container image and the release binaries are built with, and Dependabot could
  not compensate the way the workflow claimed -- it reads declared dependencies from
  `pyproject.toml` with no lockfile, so a transitive dependency of that extra was invisible to
  it too. Both scanners reported clean, and both were right about the narrower thing they were
  pointed at.

  A second audit job now resolves the full shipped dependency set -- every `kb` extra plus
  `llm-local` and `release` -- and audits that. It resolves without building anything, so the
  original flakiness argument does not apply, and it refuses to report a clean result unless the
  resolved set demonstrably contains the extras it is meant to cover: a resolution that
  silently returned nothing used to look identical to a clean scan, and now fails loudly
  instead. The `release` extra was added to the existing job for the same reason, since it
  compiles nothing.

  Consequence for anyone auditing this project: "is the dependency tree free of known
  vulnerabilities" is now a question CI can answer for the profiles that ship, rather than only
  for a subset of them. That check currently surfaces one advisory in a transitive dependency of
  `llm-local` with no fixed upstream release; it is listed explicitly in the workflow as
  known-unresolved with its disposition still open, so the job's pass/fail signal reports
  *newly appearing* advisories.
