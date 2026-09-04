# Generate documentation

`contextlake kb docs` writes documentation from the graph. Nothing is inferred, and every
statement traces to an edge that a parser recorded. No model is involved by default; `--llm`
adds one marked orientation paragraph above the API reference and changes nothing else.

`contextlake kb index` writes the same two per-repository documents itself, for the repos it
indexed, so a fresh index leaves a store with documents in it. See
[Keeping it current](#keeping-it-current).

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
| Model | optional, gated by a review council | off by default, opt in with `--llm` |
| Scope | what this repository is and does | where each symbol is used, and what its own files record about how it was built |

They are separate commands rather than two sections of one page because a document that tries
to be both serves neither.

Section by section, what lands in the reference, the design notes and the fleet page is in [What the generator produces](generated-docs-reference.md).

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
contextlake kb docs acme/forecast-api acme/alerts
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

`contextlake kb index` writes both per-repository documents for the repos it indexed, so
one command keeps the graph and the documents together:

```bash
contextlake kb index
```

It skips a repository whose head commit has not moved since its documents were written, so
running it after every commit costs two file reads per unchanged repository. It never runs a
model, and `--no-docs` switches the documents off.

`contextlake kb docs` regenerates unconditionally, which is what to reach for when a document
looks wrong. It also writes the whole-store fleet page, which `kb index` does not:
`kb index` covers the repos of one run, and a page headed "the whole store" built from part
of one would be a false claim.

```bash
contextlake kb docs
```

See [Bootstrap and keep it fresh](keeping-it-fresh.md) for running that on a schedule or on commit.

## See also

- [What the generator produces](generated-docs-reference.md)
- [Generating the wiki](generating-the-wiki.md)
- [The code graph model](code-graph-model.md)
- [Command reference](cli-reference.md)
