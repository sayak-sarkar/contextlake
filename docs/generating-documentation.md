# Generate documentation

`contextlake kb docs` writes documentation from the graph. No model is involved, nothing is
inferred, and every statement traces to an edge that a parser recorded.

Two documents per repository:

- an **API reference**, listing each symbol and the real places the codebase calls it;
- **design notes**, listing what the repository's own files record about how it was built.

Plus one for the whole store:

- a **fleet page**, listing what every repository commits to and where they disagree.

```bash
contextlake kb docs
```

Output goes to `<store>/docs/api/<repo>.md` and `<store>/docs/design/<repo>.md`, one file each
per indexed repository, and `<store>/docs/fleet/design.md` for the store as a whole.

## What makes it different from the wiki

Both read the same graph, and they answer different questions.

| | `kb wiki` | `kb docs` |
| --- | --- | --- |
| Shape | one page you read start to finish | documents you look things up in |
| Model | optional, gated by a review council | never |
| Scope | what this repository is and does | where each symbol is used, and what its own files record about how it was built |

They are separate commands rather than two sections of one page because a document that tries
to be both serves neither.

## What a reference contains

Every entry names the symbol, its kind, the line it is defined on, and its call sites:

```markdown
### `Config.from_prefixed_env`

*method, defined at line 258.*

**7 call site(s)** across **4 caller(s)**:

| Caller | File | Line | Source |
| --- | --- | --- | --- |
| `Flask.__init__` *(method)* | `src/flask/app.py` | 211 | `self.config.from_prefixed_env()` |
| `test_from_prefixed_env` *(test)* | `tests/test_config.py` | 96 | `app.config.from_prefixed_env()` |
```

Every line in that table is a line you can open.

### The quoted source is proved, not assumed

The Source column holds the actual line at the call site, which is what makes an entry an
example rather than a pointer. It is also the one part of the document that could be
confidently wrong: the graph's line numbers were recorded when the repository was indexed, and
the working tree may have moved on since.

So a line is quoted only where it can be **proved** to be the line that was indexed, meaning
the file has not been written since the index ran. Where it cannot, the cell says
`*changed since indexing*` rather than showing today's line at that number. Where nothing in
the repository can be quoted at all, the page says why once, near the top:

- the working tree is not on this machine, so the store outlived its checkout
- the repository has no `indexed_at` stamp, so there is nothing to compare against

Re-running `contextlake kb index` restores the quotes, because it moves the stamp past the
files.

### Two numbers, not one

**Call sites** and **callers** are different, and both appear because the difference matters.
A function called fifty times from inside one loop has fifty call sites and one caller. If you
are deciding whether a change is safe, you need to know which of those you are looking at.

A call site is sometimes attributed to a **file** rather than to a definition, which is what
the graph records for a call at module level with no enclosing function. Those sites are
listed, and they are not counted as callers, because a file is not something you can read to
understand the call. The count says so explicitly when it happens:

```
**124 call site(s)** across **93 caller(s)**, 4 of which name no enclosing definition
```

### Names carry their scope

A symbol is shown with its owning type when the graph recorded one, so `ostream.flush` and
`detail.glibc_file.flush` are distinguishable. Without that, a header-heavy C++ library
produces pages with several identical headings in a row and no way to tell them apart.

Where the graph did not record a scope, the bare name is shown rather than a guessed one.

## What the design notes contain

The other document answers "what was chosen here", and the honest answer is narrower than
the name suggests. A graph holds no decision records: it never sees what was rejected, or
why. What it does hold is two kinds of evidence, and the page keeps them apart because they
are not equally strong.

**Recorded** evidence is a manifest dependency. Somebody wrote `blinker>=1.9.0` in a file on
purpose, so the package, its constraint and its line are facts. Each commitment the
repository's own manifest makes at runtime becomes a numbered entry:

```markdown
### ADR-001: Depend on `blinker` at `>=1.9.0`

**Status:** proposed, never ratified.

**Decision.** `pyproject.toml:24` declares `blinker` with the constraint `>=1.9.0`,
required at runtime.

**Context.** *Nobody wrote this down. The repository records the choice and not the
reason, so what was weighed against it is not recoverable from the code.*
```

An entry states the choice and leaves the reasoning **visibly absent** rather than filling it
with a generated guess. That is the whole difference between this and a decision record: a
real ADR has Context and Consequences, and a graph can supply neither.

Only the recorded class is numbered, and only what the shallowest manifest commits to at
runtime. A dev dependency is a contributor's convenience, an optional extra is opt-in, and a
nested project's dependencies are that project's decisions. All of them stay recorded in the
tables, one table per manifest with the shallowest first, because a bundled example that
depends on this project is not a dependency of it.

"Shallowest" is usually the repository's own root manifest. In a monorepo with no top-level
manifest it is whichever sub-project sorts first, which is a tie broken by path rather than
by importance, so the entries there describe one sub-project and not the repository. The
heading names the file it read, so the page is never wrong about whose commitments it lists,
only narrower than the section title suggests.

The numbers are positions in a generated file, not stable identifiers: adding a dependency
renumbers everything after it. The page says so, and each heading names its package so there
is something stable to cite.

| Package | Constraint | Group | Line |
| --- | --- | --- | --- |
| `blinker` | `>=1.9.0` | Required at runtime | 24 |
| `asgiref` | `>=3.2` | Optional extra `async` | 33 |
| `python-dotenv` | *unpinned* | Optional extra `dotenv` | 34 |

**Inferred** evidence is a constant read in many places. That is evidence the value is
load-bearing and no evidence at all that anyone decided anything, so it stays a plain table:
the count is printed and never explained, and no constant is ever numbered as a decision. On
one measured tree, three of the seven constants that cleared the evidence bar were typing
constructs, so numbering them would have produced "ADR-005: `T` is a repository-wide type
variable" in a document whose entire claim is that it invents nothing.

Three things keep the page honest, and each exists because its absence produced a real wrong
answer somewhere:

- **Coverage is always stated**, as `N of M constants`. The filters drop candidates silently,
  and a short list with no denominator reads as "there is little here" rather than "most of
  it did not clear the bar".
- **An ambiguous reading is never counted.** Where a name has several definitions the graph
  attributes each use to all of them, so on one public tree a name defined three times
  carried an identical 41 sites on each: summing reports 123 uses of 41.
- **An empty list says what was read.** "This project declares no dependencies" and "its
  dependencies are in a file not yet read" render identically otherwise, and the second is
  real: a manifest spelling that went unread made a large application report zero.

The page also carries a machine-readable marker, because whoever reads the file receives
bytes rather than a rendered page, and a status stated only in prose is a sentence a
summariser can drop:

```markdown
<!-- contextlake:document=design status=proposed-never-ratified evidence=derived-from-code -->
```

Nothing on that page was ratified by anybody. It is a set of questions to confirm.

## What the fleet page contains

One page for the whole store, at `<store>/docs/fleet/design.md`. It answers the question no
per-repo page can, because disagreement is invisible from inside a single repository: a service
pinning `>=2.5,<4` and another leaving the same package unpinned each look reasonable on their
own page.

| Package | Repos | Manifests | Constraints in use (repos) |
| --- | --- | --- | --- |
| `queuelib` | 12 | 14 | `>=2.0` (9), `==1.8` (2), *unpinned* (1) |
| `webkit` | 7 | 7 | *unpinned* (7) |

**Every population is a count of distinct repositories, and manifests are counted separately.**
That separation is not tidiness. Measured on a real four-repository fleet, one package had 11
dependency edges across 2 repositories, because one of them declares it in eleven manifests: its
own plus ten bundled examples. Counting edges would have printed "11 repositories" onto a
four-repository fleet, which is absurd at four and perfectly plausible at forty.

The page names which packages are pinned inconsistently and then explicitly declines to
recommend anything, because a repository may pin tightly for a real reason and nothing in a
graph can tell that from drift.

The denominator names the filter it came from: "3 of 15 **packages required at runtime**", plus
how many appear only as development or opt-in dependencies. Without that, a fleet with 15
runtime and 200 development packages reads as a 15-package fleet.

Repositories absent from the tables are split by **why**, and named rather than counted:
they declare only development or opt-in dependencies (a manifest *was* read), they declare
nothing this reads, or their shard could not be loaded -- in which case the page knows nothing
about them either way and says so instead of reporting them as declaring nothing.

It is written only when a run covers the whole store. `kb docs <repo>` skips it and says why: "3
of 15 packages are shared" is a claim about the whole store, and a reader has no way to tell a
scoped page from a complete one. Only runtime and peer dependencies reach it; a dev dependency
disagreeing is a lesser finding that would bury the one that matters.

## Bounding a large repository

`--max-symbols N` caps the document; the default is 500. Symbols are **selected** by call-site
count, so the most-used interface survives the cap, and then **grouped by file**, because
that is how a reader looks something up.

```bash
contextlake kb docs --max-symbols 2000
```

Whenever the cap binds, the page says how many symbols it left out. It also says when the cut
was arbitrary: in a repository where most symbols have no recorded caller, everything at the
boundary ties at zero, and which ones were dropped was decided by filename rather than by
importance. A reference that quietly stops is worse than a short one, because you cannot tell
a symbol that is missing from a symbol that does not exist.

## Scoping to some repositories

Pass repo ids to document only those:

```bash
contextlake kb docs team/api team/worker
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every requested repository was documented, or had no symbols to document |
| `1` | A shard could not be read, or nothing could be written at all |

A repository that indexed to zero symbols is reported and is **not** a failure: a repository
of build and configuration files has no interface to document. A shard the store lists but
cannot read **is** a failure, even when other repositories succeeded, because a partial run
reported as clean is how a broken store stays broken.

## Keeping it current

The reference is generated, so regenerate it after re-indexing:

```bash
contextlake kb index && contextlake kb docs
```

See [Bootstrap and keep it fresh](keeping-it-fresh.md) for running that on a schedule or on commit.
