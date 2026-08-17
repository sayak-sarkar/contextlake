# Generate documentation

`contextlake kb docs` writes documentation from the graph. No model is involved, nothing is
inferred, and every statement traces to an edge that a parser recorded.

It writes two kinds of document per repository:

- an **API reference**, listing each symbol and the real places the codebase calls it;
- **design notes**, listing what the repository's own files record about how it was built.

```bash
contextlake kb docs
```

Output goes to `<store>/docs/api/<repo>.md` and `<store>/docs/design/<repo>.md`, one file
each per indexed repository.

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
purpose, so the package, its constraint and its line are facts. One table per manifest, the
repository's own first, because a bundled example that depends on this project is not a
dependency of it.

| Package | Constraint | Group | Line |
| --- | --- | --- | --- |
| `blinker` | `>=1.9.0` | Required at runtime | 24 |
| `asgiref` | `>=3.2` | Optional extra `async` | 33 |
| `python-dotenv` | *unpinned* | Optional extra `dotenv` | 34 |

**Inferred** evidence is a constant read in many places. That is evidence the value is
load-bearing and no evidence at all that anyone decided anything, so the page prints the
count and refuses to explain it. A generated sentence calling a type variable a core
architectural decision is worse than no sentence, and nothing in the graph can tell the two
apart.

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

See [Bootstrap and keep it fresh](keep-fresh.md) for running that on a schedule or on commit.
