# Index the code graph

Indexing turns your mirrored repos into a queryable knowledge graph. `contextlake kb index --workspace
~/work` walks every git repo under a folder and builds the graph. Runs are incremental by default;
`--force` rebuilds from scratch.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-index.png" alt="contextlake kb index --workspace output: per-repo progress bars across four acme repos, each with node and edge counts, ending in a summary of 4 repos, 29 nodes, 28 edges." width="820">
</p>

```mermaid
flowchart TD
  D(["a directory you point kb index at"]) --> B{"--bundle passed?"}
  B -->|yes| ONE["indexed as one repository"]
  B -->|no| G{"is it itself a git repo?"}
  G -->|yes| ONE
  G -->|no| S{"does it hold git repos, with<br/>nothing of yours outside them?"}
  S -->|no| ONE
  S -->|yes| R(["refused, naming the command that fits"])
  R -.->|"kb index --workspace"| EACH["each repo indexed under its own id"]
  ONE --> GR[("the graph")]
  EACH --> GR
```

<div class="dg-key">
  <i><b class="dg-sh-step"></b>a rectangle is something that runs</i>
  <i><b class="dg-sh-store"></b>a cylinder is something that persists</i>
  <i><b class="dg-sh-act"></b>a rounded box is a start or an end point</i>
  <i><b class="dg-sh-dec"></b>a diamond is a decision</i>
</div>

The shape of the directory is measured before any of it is indexed, and the next section walks each
outcome.

## Which command for which directory

`kb index <dir>` indexes that directory as **one** repository. `kb index --workspace <dir>` walks it
and indexes **each** git repository under it separately, under its own identity. Getting those two
the wrong way round is the most expensive mistake available here, so a directory that is not itself a
git repository but holds some is **refused**, with the command that fits what was found:

```
$ contextlake kb index ~/work
✗ /home/you/work isn't itself a git repo, but contains 20 git working tree(s) at depths 1-2
  (alpha, beta, billing-core, frontend, gateway, …); no indexable file lies outside them.
  That is a workspace mirroring several repositories. Indexing it this way files all of them
  under ONE id, so every symbol they hold is in the graph twice and the copies cannot be told apart.
  → Run this instead, which indexes each repository separately under its own identity:
        contextlake kb index --workspace ~/work
  → Or pass --bundle to index this directory as one repository anyway. …
```

It refuses rather than quietly switching to `--workspace` for you, because switching can lose data:
`--workspace` indexes the nested repositories and nothing outside them, so on a tree of your own
loose sources that happens to carry a dependency with its own `.git` it would index the dependency
and drop your files. So the shape is measured first, from how much indexable content lies outside
the nested repositories, and only then does it decide:

| What the directory looks like | What happens |
| --- | --- |
| Several repositories, effectively nothing of your own outside them | refused; run `--workspace <dir>` |
| One repository, nothing at all outside it | refused; the directory is one level too high, and the command names the repository |
| Real content of yours outside the repositories (your sources plus a vendored dependency) | indexed as one repository, with a line saying so |
| A git repository (`.git` present), whatever it contains | indexed, no diagnosis at all |

**`--bundle` opts back in.** It indexes the directory as one repository regardless of shape, and it
is read before anything is measured, so it always works. Reach for it when you genuinely want one
bundled repository -- and note the cost you are accepting: the nested repositories' files are filed
under the directory's name, so if you later index them properly they are in the graph twice. `kb
forget <repo-id>` removes a bundle you did not mean to create.

## Incremental and time-travel

`index --workspace` is **incremental**, it re-indexes only repos whose git HEAD moved since their last
index, so a scheduled (cron) run stays cheap; pass `--force` to rebuild everything, or `--watch [--interval
N]` to keep re-indexing in a loop (the same `--watch` / `--interval` flags also drive `connect` and
`embed`). Every indexed snapshot is kept, so `query "<text>" --repo R --as-of <commit>` does
**time-travel**, it searches repo `R` as it was at a previously-indexed commit.

## Re-indexing one repository by its id

A repository's **id** is derived from its `origin` remote (so it survives being moved or re-cloned) and has
no relation to where the clone sits on disk. That is the id `kb lint` and the dashboard report, so
`--source` accepts it directly:

```bash
contextlake kb index --source example.com/team/widgets   # the id, exactly as reported
contextlake kb index --source widgets                    # the tail, when only one repo ends that way
```

The repository is re-indexed under the id it was already filed as, never a second time under its directory
name. If the id is unknown, the error names near-miss ids from the store; if it is known but its recorded
checkout is gone, the error names the path it was indexed from.

For a checkout that has no usable `origin`, or one you want filed under a different name, `--repo` sets
the id explicitly: `contextlake kb index --source ./widgets --repo team/widgets`. Without it the id
falls back to the directory name, which is exactly the fragile, path-dependent id this section exists to
avoid, so name it once rather than living with a repo filed as `widgets` because that is what the folder
happened to be called.

## Parallelism and noise-pruning

Repositories are parsed across **worker processes** (CPU-bound work) while the SQLite store is written
serially from the parent. The `spawn` start method is used on every platform, so behaviour is identical on
Linux, macOS, and Windows, with an automatic serial fallback if a worker pool can't start. It defaults to
`cpu_count - 1` workers (capped at 8); set `[kb] index_workers` to tune it (`1` forces serial).

The parser also **skips machine-generated and derived files**: names like `*.designer.cs`, `*.min.js`
and `AssemblyInfo.cs`, and any of four case-insensitive header markers in the first 2048 bytes,
`<auto-generated`, `@generated`, `do not edit` and `code generated by`. That last pair catches more than
it looks: a file whose banner just says "DO NOT EDIT" is skipped too. Also skipped are code files larger than `[kb]
max_file_bytes` (5 MB). That's derived noise, not real source, and every skip is reported (no silent gaps).
Set `[kb] skip_generated = false` or raise `max_file_bytes` to index them anyway. Discovery also skips
**vendored nested repos**: an upstream clone carried inside the mirror with its own `.git` under a
`module-federation` path segment, which is not your source and would flood the global graph with
upstream-demo nodes. Each such skip is logged. (`node_modules` trees are already pruned before discovery
descends into them.)

To exclude your own paths, drop a **`.contextlakeignore`** at a repo's root: one glob per line (`#`
comments and blank lines ignored), matched against each file's path relative to the repo and its name, so
`*.lock` ignores by name anywhere and `vendor/` ignores a directory and everything under it. It's a small,
dependency-free subset of gitignore syntax (no negation, `**`, or anchoring), enough to drop vendored trees
and lockfiles from the graph.

## Health and maintenance

`contextlake doctor` checks the environment (FTS5, `git` / `glab` on PATH, the store, the embedder, and the
ANN index) and exits non-zero if anything is wrong, so it doubles as a CI health gate. It also flags (advisory,
doesn't affect the exit code) any shard indexed with an older parser version than the one currently
installed -- `contextlake kb index` rebuilds those on its next run, so they pick up parser-correctness fixes.
`contextlake kb lint` audits the graph itself, reporting **stale repos** (HEAD moved since they were
indexed), **dangling edges** (an edge whose endpoint node is missing), and the same **older-parser** repos
doctor reports, so the two commands never disagree about one store. Both exit non-zero on problems -- for
lint that means dangling edges, HEAD-stale repos, or repos it cannot read; the older-parser count is reported
but deliberately kept out of its exit code, so upgrading to a build with a new parser can't turn a green CI
gate red on its own.

Two states are reported apart from stale, because re-indexing clears a stale repository and cannot clear
either of them:

- **empty** -- the repository has no commits at all, so there is no HEAD and nothing to index. Reported,
  and not counted against the exit code: there is nothing for a reader to do about it.
- **shard-imported** -- indexed from a graph-shard JSON rather than a checkout, so it has no history to
  be behind. Also advisory, for the same reason.
- **unreadable** -- the recorded path no longer exists, or git will not answer for a repository there.
  Re-clone it or drop it from the store. This one does fail the run, because nothing can be cited from it.

The full node and edge model, language by language, is in [The code graph model](code-graph-model.md).

## See also

- [Connect and enrich](connecting-and-enriching.md)
- [Semantic search](searching-semantically.md)
- [Serve it to your editor](serving-over-mcp.md)
