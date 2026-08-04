# Developer convenience wrapper around the exact commands CI runs (see
# .github/workflows/ci.yml) so a local `make test`/`make lint` and the CI gate
# can't drift apart. Every recipe below is one of that file's `run:` lines,
# copy-pasted, not reinvented.
#
# Manages its own .venv (same two commands CONTRIBUTING.md documents doing by
# hand: `python -m venv .venv && source .venv/bin/activate && pip install ...`)
# so `make install && make test` works turnkey on a clean clone with no venv
# pre-activated. If you already have your own venv active, the plain commands
# in CONTRIBUTING.md's "The loop" section work too -- this file doesn't
# require using it.

VENV      := .venv
PIP       := $(VENV)/bin/pip
PYTEST    := $(VENV)/bin/pytest
RUFF      := $(VENV)/bin/ruff

.PHONY: install install-core lint lint-fix test test-core test-fast coverage clean distclean

# Full dev setup: the CLI + pytest/ruff + the knowledge layer the kb tests
# need. Matches CONTRIBUTING.md's primary "Getting set up" command and CI's
# kb job install step.
install: $(VENV)/.stamp
	$(PIP) install -e ".[dev,kb]"

# Core-only setup (no knowledge-layer deps): matches CI's core job install
# step and CONTRIBUTING.md's Python-3.9 / core-only alternative. Use with
# `make test-core`, since tests/kb requires the [kb] extra.
install-core: $(VENV)/.stamp
	$(PIP) install -e ".[dev]"

$(VENV)/.stamp:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	touch $@

# CI: `ruff check src tests` (core job and kb job both run this, identically).
lint:
	$(RUFF) check src tests

# Not a CI step -- the auto-fix half of CONTRIBUTING.md's documented loop.
lint-fix:
	$(RUFF) check --fix src tests

# CI: the kb job's full-suite step, coverage floor included. Requires
# `make install` (needs the [kb] extra); use `make test-core` if you only
# ran `make install-core`. Deliberately serial, matching CI exactly -- don't
# add -n here (see test-fast for why xdist stays opt-in, not the default).
test:
	$(PYTEST) --cov=contextlake --cov-report=term-missing --cov-fail-under=88

# CI: the core job's step -- skips tests/kb, no coverage floor (see
# ci.yml's comment: the floor lives only on the full-suite job on purpose,
# so a narrow `pytest -k ...` run's partial coverage number never "fails").
test-core:
	$(PYTEST) --ignore=tests/kb --cov=contextlake --cov-report=term-missing

# Fast inner loop for active development: skip the tests marked `slow` (see
# pyproject.toml's marker registration) and run the rest across CPU cores via
# pytest-xdist. Not a CI step and not the default `test` target -- opt in
# explicitly. Measured on this suite: ~90s serial -> ~22s with `-m "not
# slow" -n auto` (shared reference venv, 22 cores). Verified parallel-safe
# empirically (three consecutive clean -n auto full-suite runs, identical
# pass/xfail counts each time) because every test that binds a real socket
# uses an OS-assigned ephemeral port (`_free_port()`) and pytest's per-test
# tmp_path, never a hardcoded port or shared path -- but `_free_port()`'s own
# bind-close-then-rebind-elsewhere is a TOCTOU window that widens as worker
# count grows, which is exactly why this stays an opt-in target instead of
# pyproject.toml's addopts.
test-fast:
	$(PYTEST) -m "not slow" -n auto

# Same coverage gate as `make test`, plus a browsable HTML report
# (htmlcov/index.html) for finding uncovered lines locally. Also
# deliberately serial -- see `test`.
coverage:
	$(PYTEST) --cov=contextlake --cov-report=term-missing --cov-report=html --cov-fail-under=88

# Removes generated caches/reports only -- keeps .venv, so re-running a test
# target right after doesn't have to reinstall the [kb] extra's tree-sitter
# grammars from scratch. Use `make distclean` to also drop the venv.
clean:
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage dist build *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV)
