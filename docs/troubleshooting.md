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

## `pip install "contextlake[llm-local]"` tries to compile C++

```
Building wheel for llama-cpp-python (pyproject.toml) ... error
CMake Error: could not find cmake ...
```

Expected on every platform and every Python version, and only on a **pip**
install. The standalone binary already carries the index in its bootstrap
configuration (it installs the runtime on first run), and the full Docker image
ships it baked in, so neither hits this.

`llama-cpp-python` publishes **no wheels to PyPI at all**, every release is a
source tarball, so pip has nothing to install but the sources and falls back to
compiling `llama.cpp`, which needs `cmake` plus a C++ toolchain. That is not
neglect upstream: llama.cpp is compiled per hardware backend, and one PyPI
namespace cannot hold the CPU, CUDA and Metal builds of the same version, so
upstream ships one index per accelerator instead (the convention PyTorch uses).

Let contextlake attach the CPU index for you:

```bash
contextlake doctor --fix llm-local          # --dry-run prints the command and stops
```

Or by hand:

```bash
pip install "contextlake[llm-local]" --only-binary llama-cpp-python \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

`--only-binary llama-cpp-python` makes pip refuse a source build for that one
package, so a genuinely missing wheel is a one-line error instead of a
compiler-error wall. It names the package rather than using `:all:` on purpose:
`:all:` would forbid a source fallback for every other dependency too. For a GPU
build, swap the URL for `.../whl/cu124` or `.../whl/metal`.

contextlake cannot bake the index into the `[llm-local]` extra: PEP 508 has no
field for one, deliberately, so only a `pip` command line can add it. If you would
rather skip the native build entirely, use Ollama for the wiki tier
(`--llm ollama`).

## `contextlake doctor --fix` says the environment is externally managed

```
error: externally-managed-environment
```

Your distribution marked the system Python as managed by its own package manager
(PEP 668), and pip refuses to write into it. `--fix` reports this rather than
retrying, because the fix is where contextlake lives, not which flag you pass:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install "contextlake[kb-full]"
```

or let `pipx` own the environment: `pipx install "contextlake[kb-full]"`. Never
reach for `--break-system-packages`; it does what it says.

## `doctor --fix` printed a `sudo` command instead of running it

Working as designed. `--fix` installs **Python** packages into the current
interpreter unattended, but a **system** package (git, a C++ toolchain) needs
administrator rights, so it is only ever offered with a y/N prompt at a real
terminal. Without a TTY, or with `--skip-interactive`, the exact command is
printed and nothing privileged runs. That keeps a CI job or a scripted run from
ever tripping a sudo prompt. Copy the printed command, run it yourself, and
re-run `contextlake doctor`.

## Tests are slow locally

Run the fast subset while iterating:

```bash
make fast
```

That skips the tests marked `slow` (wiki generation, a real server lifecycle
poll loop) and runs the rest in parallel. Run the full suite with `make test`
before you push, since that is what CI runs.
