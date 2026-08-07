### Added

- **`kb index --bundle`**, to index a directory that holds git repositories as one repository
  anyway. It is the opt-in half of the refusal below, and it is read before the directory's shape
  is measured at all, so it always works.

### Changed

- **`kb index <dir>` now refuses a directory that holds git repositories, instead of warning and
  indexing it anyway.** The warning was correct and it printed the right command, and it was still
  not enough: a warning is one keystroke from being scrolled past. On one real store it was scrolled
  past, and the result was a pseudo-repository holding a duplicate copy of every mirrored
  repository -- 63% of all nodes in the store, and every symbol in the graph present twice under two
  identities that could not be told apart. `kb embed` then wrote 91% of its vectors into the
  duplicate.

  It refuses rather than quietly switching to `--workspace` for you, because switching can lose
  data. `--workspace` indexes each nested repository and nothing outside one, so on a tree of your
  own loose sources that happens to carry a dependency with its own `.git` it would index the
  dependency and silently drop your sources -- strictly worse than the bundling it replaced, which
  at least captured them. So the shape is measured first, from how much indexable content lies
  outside the nested repositories, and the refusal prints what was found (how many working trees, at
  what depths, how much content outside them), which shape that indicates, the one command that fits
  it with the real path in it, and why `--bundle` exists. It exits non-zero.

  Three shapes, three answers. Several repositories with effectively nothing of your own outside
  them is a workspace mirror, and the command is `--workspace <dir>`. One repository with nothing at
  all outside it means the directory is one level too high, and the command names that repository.
  Real content of yours outside the repositories is a project carrying a dependency, and that is
  bundled as before, now with a line saying so rather than in silence. A directory that is itself a
  git repository never reaches the diagnosis at all, however many checkouts it contains, so the
  ordinary `kb index .` is untouched.
