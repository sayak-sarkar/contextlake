# Troubleshooting

Setup problems that have actually been hit, with the fix. If you hit something
that isn't here, please open an issue: a reproducible failure is worth more to
this file than a guess.

## `pip install -e ".[kb]"` fails with "Cannot uninstall PyJWT"

```
Cannot uninstall PyJWT, RECORD file not found.
Hint: The package was installed by debian.
```

Debian and Ubuntu ship some Python packages through `apt`, and pip refuses to
uninstall what it did not install, because it cannot tell which files belong to
the distro copy.

Install into a virtual environment, which is the fix rather than the workaround:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,kb]"
```

If you genuinely need the system interpreter, tell pip to leave the distro copy
alone:

```bash
pip install -e ".[kb]" --ignore-installed PyJWT
```

## `python -m pytest` used to fail from the repo root

Historical, fixed. The repo-root launcher was named `contextlake.py`, and
`python -m` puts the working directory first on `sys.path`, so that file shadowed
the installed `contextlake` package and every import failed before a single test
collected. It is now `run-contextlake.py`, and CI runs
`python -m pytest --collect-only` to keep it that way.

If you are on an older checkout and see
`ModuleNotFoundError: No module named 'contextlake.cli'`, that is this, and
updating fixes it.

## `pre-commit run --all-files` wants to change files I didn't touch

Expected once, on a clone predating the hooks. `trailing-whitespace` and
`end-of-file-fixer` were applied repo-wide in a single commit when they were
introduced, so on current `main` they are no-ops. If you see a large diff, you
are likely on an older branch; rebase first.

Note both hooks deliberately skip `tests/kb/fixtures/fuzz/` and
`tests/kb/golden/`. Those files' exact bytes are the test: the fuzz corpus is
adversarial input for the parser timeout tests, and the golden shard is a
byte-compared snapshot of indexer output. Reformatting either quietly changes
what is being asserted.

`ruff-format` is registered but not wired into a normal commit: the repo has
never been formatted with it, and enabling it needs its own one-time, repo-wide
commit first. Run it explicitly if you want to see what it would do:

```bash
pre-commit run ruff-format --hook-stage manual --all-files
```

## The knowledge layer will not install

`[kb]` needs Python 3.10 or newer, because the `mcp` SDK does. The mirror side
of contextlake supports 3.9, so an install can resolve and then fail at import
if you are on 3.9. Check with `python3 --version`.

The extra also pulls a tree-sitter grammar per supported language, several with
native wheels. On a platform without prebuilt wheels those build from source,
which is slow rather than broken.

## Tests are slow locally

Run the fast subset while iterating:

```bash
make fast
```

That skips the tests marked `slow` (wiki generation, a real server lifecycle
poll loop) and runs the rest in parallel. Run the full suite with `make test`
before you push, since that is what CI runs.
