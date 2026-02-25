# Contributing to contextlake

First off — thanks for taking the time. `contextlake` is a small, focused tool,
and small focused tools live or die by their sharp edges staying sharp. Bug
reports, fixes, and well-scoped features are all welcome.

## Ground rules

- **Keep it lean.** This tool does one thing: mirror the GitLab repositories you
  can access and keep each on its most active branch. Features that don't serve
  that mission are a hard sell.
- **No network in tests.** Everything that shells out to `git` or `glab` must be
  faked. A passing test suite should never touch GitLab.
- **Every change ships with a test.** Bug fix? Add the test that fails without
  it. Feature? Cover the happy path and the obvious failure.

## Getting set up

```bash
git clone https://github.com/sayak-sarkar/contextlake.git
cd contextlake
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # installs the CLI plus pytest + ruff
```

You'll also want `git` and an authenticated [`glab`](https://gitlab.com/gitlab-org/cli)
on your PATH to exercise the tool for real (`glab auth login`).

## The loop

```bash
ruff check src tests          # lint
ruff check --fix src tests    # …and auto-fix what it can
pytest                        # run the suite
pytest --cov=contextlake --cov-report=term-missing   # with coverage
pytest tests/test_clone.py -k retries -q             # a single test
```

CI runs exactly `ruff check` + `pytest` across Python 3.9–3.13, so if those two
pass locally you're in good shape.

## How the code is laid out

```
src/contextlake/
├── cli.py            argument parsing + command dispatch (thin)
├── core.py           the real work: fetch / clone / update / branches / verify
├── config.py         INI loading, precedence, path expansion
├── safety.py         working-branch and clean-workspace protection
└── logging_setup.py  one logger, console + optional rotating file
```

The CLI stays thin: it parses, resolves config, and calls into `core`. Business
logic belongs in `core` (and is unit-testable without a real repo). Anything that
could clobber a developer's local work goes through `safety`.

When adding a command or option:

1. Wire the flag in `cli.build_parser()` (tri-state booleans default to `None` —
   see the comment there for why that matters).
2. Implement the behaviour in `core` as a small, testable function.
3. Add tests using the `fake_subprocess` fixture (see `tests/conftest.py`).

## Commit style

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) with
a scope:

```
<type>(<scope>): <subject>

type:  feat | fix | docs | test | refactor | chore | ci | build | perf | style
scope: cli | core | config | safety | fetch | clone | update | branches | verify | logging | docs | ci
```

Examples:

```
fix(config): expand ~ in config-file work_dir
feat(branches): add recency-aware branch strategy
test(clone): cover corrupted-directory cleanup
```

Keep commits **atomic** — one logical change each. A commit that "fixes the bug
and also reformats four files" is two commits wearing a trenchcoat.

## Submitting a change

1. Branch off `main`: `git switch -c fix/<short-description>`.
2. Make the change, add tests, keep `ruff` and `pytest` green.
3. Update `CHANGELOG.md` under `[Unreleased]` if the change is user-visible.
4. Open a PR describing **what** changed and **why**. Link any issue.

## Reporting bugs

Open an issue with: what you ran, what you expected, what happened, and the
output (scrub any private group names, URLs, or tokens first). A failing test
case is the gold standard.

## Security

Please don't file security issues in public. See [SECURITY.md](SECURITY.md).
