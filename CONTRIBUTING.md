# Contributing to contextlake

First off, thanks for taking the time. `contextlake` is a small, focused tool,
and small focused tools live or die by their sharp edges staying sharp. Bug
reports, fixes, and well-scoped features are all welcome.

## Ground rules

- **Keep it lean.** contextlake is three layers you adopt one at a time: mirror
  your repos, build an optional knowledge graph over them, serve that graph to
  AI tools over MCP. Features that don't serve one of those three jobs are a
  hard sell.
- **No network in tests.** Everything that shells out to `git` or `glab` must be
  faked. A passing test suite should never touch GitLab.
- **No real filesystem/dotfile mutation in tests, either.** `tests/conftest.py`'s
  autouse `_isolated_home` fixture hard-redirects `HOME` to a per-test tmp
  directory for the *entire* suite specifically because of a real incident: a
  code path that had always been a no-op (declining shell-completion setup)
  gained a real write (a decision marker, so a later automatic check never
  re-asks), and every pre-existing test that exercised it, never having
  needed to isolate `HOME` before, silently started writing to the real
  machine's `~/.contextlake/`. **When a branch that used to do nothing gains a
  filesystem or env-dependent side effect, re-check every test that already
  exercises it, not just the new tests you're adding for it.**
- **Every change ships with a test.** Bug fix? Add the test that fails without
  it. Feature? Cover the happy path and the obvious failure.
- **No secrets or local config in the repo.** Never hardcode credentials, API
  keys, tokens, or absolute/local filesystem paths. Use generic placeholders
  (`frontend/*`, `auth-service`, `user@example.com`, `~/…`), and read anything
  environment-specific from config or the environment at runtime.

## Getting set up

```bash
git clone https://github.com/sayak-sarkar/contextlake.git
cd contextlake
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,kb]"   # the CLI + pytest/ruff + the knowledge layer the kb tests need
# core-only (no kb deps): pip install -e ".[dev]" and run pytest --ignore=tests/kb
```

You'll also want `git` and an authenticated [`glab`](https://gitlab.com/gitlab-org/cli)
on your PATH to exercise the tool for real (`glab auth login`).

Install the hooks once per clone (see [Pre-commit hooks](#pre-commit-hooks) for what they
do and deliberately don't do):

```bash
pre-commit install
```

The venv in that first block is not a formality. On Debian and Ubuntu, installing into the
system interpreter fails with:

```
Cannot uninstall PyJWT, RECORD file not found.
Hint: The package was installed by debian.
```

Those distros ship some Python packages through `apt`, and pip refuses to uninstall what it
did not install, because it cannot tell which files belong to the distro copy. A virtual
environment sidesteps it entirely. If you genuinely need the system interpreter, tell pip to
leave the distro copy alone with `pip install -e ".[dev,kb]" --ignore-installed PyJWT`.

## The loop

```bash
ruff check src tests          # lint
ruff check --fix src tests    # …and auto-fix what it can
pytest                        # run the suite
pytest --cov=contextlake --cov-report=term-missing   # with coverage
pytest tests/test_clone.py -k retries -q             # a single test
```

`ruff check` includes `S` (flake8-bandit), so a security finding fails the lint rather than
being reported where nobody reads it.

If one fires on your change, **read it rather than silencing it.** The backlog was triaged to
zero, and every finding still in the tree carries a `# noqa: S... - <why it is safe>` on its own
line.

Follow that pattern: one site, one written reason. A blanket ignore, or adding a rule to
`per-file-ignores`, undoes the property the gate exists for.

Two rules are already off for the package, `S603` and `S607`, because they fire on every
subprocess this tool exists to run. `pyproject.toml` says why.

The full suite takes a while. While you're iterating, run the fast subset instead:

```bash
make test-fast                # skips the tests marked `slow`, runs the rest via pytest-xdist
```

`slow` covers wiki generation and a real server-lifecycle poll loop. Run `make test` (which
is what CI runs, serially, with the coverage floor) before you push. `make` targets manage
their own `.venv`; if you already have one activated, the plain commands above work too.

`pytest` and `python -m pytest` both work from the repo root. The bare-script
launcher lives at `run-contextlake.py`, a name deliberately chosen so it can
never collide with the installed `contextlake` package on `sys.path`. On an older
checkout, where that file was still named `contextlake.py`, `python -m pytest` fails with
`ModuleNotFoundError: No module named 'contextlake.cli'`, because `python -m` puts the
working directory first on `sys.path` and the launcher shadowed the installed package.
Updating fixes it, and CI runs `python -m pytest --collect-only` to keep it fixed.

CI runs exactly `ruff check` + `pytest` across Python 3.10-3.13, one matrix shared by
the core and knowledge-layer jobs, so if those two
pass locally you're in good shape.

## Pre-commit hooks

`pre-commit install` wires in `ruff-check --fix` plus `trailing-whitespace` and
`end-of-file-fixer`. It's a convenience net for the cheap stuff, not a replacement for CI.

If `pre-commit run --all-files` wants to change files you never touched, you're probably on
a branch that predates the hooks: the two whitespace fixers were applied repo-wide in a
single commit when they were introduced, so on current `main` they are no-ops. Rebase first.

Both fixers deliberately skip `tests/kb/fixtures/fuzz/` and `tests/kb/golden/`, because those
files' exact bytes *are* the test. The fuzz corpus is adversarial input for the parser
timeout tests, and the golden shard is a byte-compared snapshot of indexer output;
reformatting either quietly changes what is being asserted.

`ruff-format` is registered but staged `manual`, so it never runs on a normal commit. The
repo has never been formatted with it and CI has no `ruff format --check` step, so enabling
it for real needs its own one-time, repo-wide commit first. Run it explicitly to see what it
would do:

```bash
pre-commit run ruff-format --hook-stage manual --all-files
```

## How the code is laid out

```
src/contextlake/
├── cli.py            argument parsing + command dispatch (thin)
├── core.py           the real work: fetch / clone / update / branches / verify
├── config.py         INI loading, precedence, path expansion
├── safety.py         working-branch and clean-workspace protection
├── init_cmd.py        `init` + shell-completion setup/auto-registration
└── logging_setup.py  one logger, console + optional rotating file
```

(The optional `[kb]` extra's knowledge-layer package, `src/contextlake/kb/`, has its own much
larger internal layout, see [docs/internals.md](docs/internals.md), not covered here.)

The CLI stays thin: it parses, resolves config, and calls into `core`. Business
logic belongs in `core` (and is unit-testable without a real repo). Anything that
could clobber a developer's local work goes through `safety`.

When adding a command or option:

1. Wire the flag in `cli.build_parser()` (tri-state booleans default to `None`;
   see the comment there for why that matters).
2. Implement the behaviour in `core` as a small, testable function.
3. Add tests using the `fake_subprocess` fixture (see `tests/conftest.py`).
4. A brand-new top-level command also needs one entry in `cli._COMMAND_CATEGORIES`
   (which category it belongs to for `contextlake --help`'s grouped listing).
   `test_every_registered_command_is_categorized_exactly_once` fails loudly if
   you forget, so this is hard to miss, not a silent gap.

## Commit style

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) with
a scope:

```
<type>(<scope>): <subject>

type:  feat | fix | docs | test | refactor | chore | ci | build | perf | style
scope: whatever the change is actually about -- cli | core | config | safety | fetch | clone |
       update | branches | verify | logging for the mirror core; kb | graph | wiki | serve |
       dashboard | connect | connectors | embed | ingest | enrich for the optional knowledge
       layer; docs | site | release | test | ci apply across both. Not a fixed enum: a scope
       narrow enough to search for later beats forcing a change into the nearest listed one.
```

Examples:

```
fix(config): expand ~ in config-file work_dir
feat(branches): add recency-aware branch strategy
test(clone): cover corrupted-directory cleanup
```

A scope is **required**. `docs:` is not a valid subject; `docs(readme):` is.

### The subject is a heading, not a sentence

The subject answers one question: **what does this changeset do?** Write it in the
imperative, lowercase after the colon, 72 characters or fewer, no trailing full stop.
Everything else (why, what was wrong before, what you measured, what you rejected) goes
in the body.

A subject that reads like a line lifted out of a paragraph is the failure to avoid:

```
docs: the 8.7.0 preamble said three defects, and there are four
```

That says what was wrong with the old text, not what the commit did about it. It also
drops the scope. The fix:

```
docs(changelog): correct the 8.7.0 defect count and characterisation
```

The recurring failure modes, with the shape to use instead:

| Instead of | Write |
|---|---|
| stating the bug: `fix(kb): index --watch did nothing without --workspace` | `fix(kb): honour --watch when --workspace is absent` |
| narrating: `docs(releasing): re-derive step 1 from the workflows, again` | `docs(releasing): re-derive step 1 from the CI workflows` |
| being clever: `docs: make the style guide obey itself` | `docs(style-guide): apply the em-dash rule to the guide itself` |
| a bare noun phrase: `feat(schedule): pure interval recommender` | `feat(schedule): add the interval recommender` |
| being vague: `chore: cleanup` | `chore(tests): remove the unused fixture helpers` |

Stating the bug is the worst of these, because in a blame view it reads as though the
commit introduced the problem rather than fixed it.

A useful check: if you cannot write the subject without a comma joining two ideas, you
probably have two commits.

Read the subject aloud before committing. If it sounds like the title of a sitcom episode,
something chosen to be enjoyable rather than to inform, rewrite it as a statement of the
change.

### Do not round the prose

A commit message records what changed. It is not an essay. Do not shape it for rhythm,
build to a point, or end a paragraph with a judgement. One fact per sentence, and name the
function, file, flag or number.

| Rounded | Factual |
|---|---|
| `Two properties shape the module. append_run never raises, because...` | `append_run() catches OSError, TypeError and ValueError and returns.` |
| `...and throwing away every good measurement to punish one bad one is the wrong trade.` | `read_runs() skips lines that fail to parse and returns the rest.` |
| `The module is pure, which is what makes every branch testable.` | `The module has no I/O. Every value is passed in.` |

Delete essay scaffolding (`Two properties shape...`, `Three things follow from this...`),
closing verdicts (`...which is the whole point`), and the `not X, but Y` contrast. A body
that reads slightly flat is correct.

### The body is a record, not a conversation

A commit body has no reader to reply to, so do not write it as though continuing a
discussion. Do not open a sentence with `Exactly`, `Precisely`, `Right`, `Turns out`,
`Which is why`, `That said`, `Of course`, `Note that`, or `Indeed`. State the fact.

| Conversational | Written |
|---|---|
| `Exactly the class 8.6.1 existed to fix: shipped text that does not match what shipped.` | `8.6.1 fixed the same class of defect: shipped text that does not match what shipped.` |
| `Turns out the gate never ran.` | `The gate never ran.` |
| `Note that this file is generated.` | `This file is generated.` |

`exactly` is worth a second look wherever it appears, including as an intensifier
(`exactly what`, `exactly this`, `exactly why`). It can usually be deleted without loss.

Keep commits **atomic**, one logical change each. A commit that "fixes the bug
and also reformats four files" is two commits wearing a trenchcoat.

## Changing the documentation

The `docs` and `site` scopes above have rules of their own, and two of them will bite you if you
skip them.

**Write to the style guide.** [docs/style-guide.md](docs/style-guide.md) is the yardstick, split
across [voice](docs/style-guide-voice.md), [page types and
structure](docs/style-guide-structure.md), [formatting and
accessibility](docs/style-guide-formatting.md), and the [word and term
reference](docs/style-guide-reference.md). One rule is enforced by a test rather than by review:
em-dashes are rejected outright in `docs/**/*.md`, `README.md`, `QUICKSTART.md`, `CONTRIBUTING.md`
and `ROADMAP.md`, outside fenced code blocks. The rest is on you, starting with "contextlake" being
one lowercase word everywhere, even at the start of a sentence.

**Register a new page, or nothing links to it.** The site is generated by
[site/build_docs.py](site/build_docs.py) from an explicit page list. A new `docs/*.md` that is not
added to `PAGES` (or deliberately to `TO_GH`) is unreachable from the site nav, and the build fails
rather than shipping it. Run the build after any docs change, because `site/llms.txt` and
`site/llms-full.txt` are generated from the same list and a test compares them against the source:

```bash
python site/build_docs.py
```

Never hand-edit anything under `site/*.html`; it is generated.

## Submitting a change

1. Branch off `main`: `git switch -c fix/<short-description>`.
2. Make the change, add tests, keep `ruff` and `pytest` green.
3. Update `CHANGELOG.md` under `[Unreleased]` if the change is user-visible.
4. Open a PR describing **what** changed and **why**. Link any issue.

## Releasing

Maintainers: see [docs/releasing.md](docs/releasing.md) for the full runbook:
version bump, changelog, tag, build, and publishing to PyPI (including the
first-token and corporate-proxy gotchas). Install the tooling with
`pip install -e ".[release]"`.

## Reporting bugs

Open an issue with: what you ran, what you expected, what happened, and the
output (scrub any private group names, URLs, or tokens first). A failing test
case is the gold standard. If the problem is with *using* contextlake rather than
developing it, check [docs/troubleshooting.md](docs/troubleshooting.md) first, since the
common install and mirror failures are already written up there with their causes.

## Security

Don't file security issues in public. See [SECURITY.md](SECURITY.md).

## See also

- [Quickstart](QUICKSTART.md)
- [Adding a language](docs/adding-a-language.md)
- [Releasing and publishing](docs/releasing.md)
- [Documentation style guide](docs/style-guide.md)
- [Security policy](SECURITY.md)
