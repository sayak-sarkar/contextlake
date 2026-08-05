"""Shared pytest fixtures for contextlake.

The tool shells out to ``git`` and ``glab``; tests must never touch the network
or a real GitLab. ``fake_subprocess`` swaps ``core.subprocess.run`` for a
programmable stub, and ``no_sleep`` makes retry/backoff instant.
"""

import logging
import subprocess
import types

import pytest

from contextlake import core, observability


@pytest.fixture
def gls_logs(caplog):
    """Capture the contextlake logger's records regardless of propagation.

    The package logger sets propagate=False, so caplog's root handler misses it;
    attaching caplog's handler directly to the logger captures reliably.
    """
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("contextlake")
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.fixture(autouse=True)
def reset_logging():
    """Clear the package logger before each test.

    The StreamHandler binds to sys.stdout at construction, so handlers left over
    from a previous test would point at a stale stream and defeat capsys. With
    handlers cleared, the first log() call in a test rebuilds against the current
    (captured) stdout.
    """
    logging.getLogger("contextlake").handlers.clear()
    yield
    logging.getLogger("contextlake").handlers.clear()


@pytest.fixture(autouse=True)
def reset_redactions():
    """Clear the process-wide log-redaction rules between tests.

    They are deliberately process-global (one CLI process = one run), and the
    fleet-listing functions register repo names as a side effect -- so without
    this, one test's fixture repo names would rewrite another test's output.
    """
    observability.reset_redactions()
    yield
    observability.reset_redactions()


class FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeSubprocess:
    """Records calls and dispatches each to a configurable handler.

    Set ``.handler`` to a callable ``(cmd: list[str], **kwargs) -> FakeCompleted``.
    The default handler returns a successful empty result. Every invocation is
    appended to ``.calls`` for assertions.
    """

    def __init__(self):
        self.calls = []
        self.handler = lambda cmd, **kwargs: FakeCompleted()

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        result = self.handler(list(cmd), **kwargs)
        if result is None:
            return FakeCompleted()
        return result

    def commands_matching(self, *needles):
        """Return calls whose argv contains all of the given substrings."""
        out = []
        for cmd in self.calls:
            joined = " ".join(cmd)
            if all(n in joined for n in needles):
                out.append(cmd)
        return out


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Point core.subprocess at a programmable fake (no real processes spawned).

    Only the ``core`` module's view of ``subprocess`` is swapped; the real
    TimeoutExpired/CalledProcessError types are re-exposed so the module's
    ``except subprocess.X`` clauses still work.
    """
    fake = FakeSubprocess()
    monkeypatch.setattr(
        core,
        "subprocess",
        types.SimpleNamespace(
            run=fake.run,
            TimeoutExpired=subprocess.TimeoutExpired,
            CalledProcessError=subprocess.CalledProcessError,
        ),
    )
    return fake


@pytest.fixture(autouse=True)
def _clean_gitlab_env(monkeypatch):
    """Keep tests hermetic: the developer's own GITLAB_TOKEN/GITLAB_HOST (and any
    RES_OPTIONS) must not leak in and silently switch the fetch path or hit the
    network. Tests that want a token set it explicitly via monkeypatch.setenv.
    """
    for var in ("GITLAB_TOKEN", "GITLAB_HOST", "RES_OPTIONS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Hard backstop: no test should ever be able to write to the real
    machine's actual home directory (dotfiles, ~/.contextlake/, etc.),
    regardless of which code path it exercises or whether it remembered to
    isolate HOME itself. This was a real, observed leak: `init_cmd`'s decline
    path writes a `~/.contextlake/.completion_setup_done` marker (needed so
    the zero-step auto-check never re-asks), and dozens of pre-existing
    `cmd_init`-based tests only ever redirected `CONFIG_FILE`/`_KB_CONFIG`
    (module attributes), never the `HOME` env var itself -- safe when that
    decline path was a no-op, not once it started writing a file. A test that
    sets HOME explicitly to its own tmp_path still wins (its own
    monkeypatch.setenv call runs later, in the test body, after this fixture).
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    # HOME alone is not the whole of "the user's home": XDG_CACHE_HOME, when the
    # developer or CI runner exports it, sends the mirror project cache (see
    # config.get_cache_paths, which creates and chmods that directory) straight
    # back out to the real machine. Clear it here so the backstop actually is one;
    # a test that wants a specific value still sets it in its own body.
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)


@pytest.fixture(autouse=True)
def _no_auto_completion(monkeypatch):
    """Keep tests hermetic: `maybe_auto_register_completion` (see cli.py's
    main()) fires on every real CLI invocation gated only on isatty() and a
    one-time marker -- under `pytest -s`, or any runner that leaves a TTY
    attached, an unguarded test calling cli.main(...) would register
    completion against the DEVELOPER'S OWN real ~/.zshrc and create a real
    ~/.contextlake/. This is the exact hazard test_init.py's own `_args()`
    already documents guarding against for `contextlake init` directly; this
    fixture closes the same hole for the newer zero-step auto-check. Tests
    that specifically exercise the auto-check opt back in via
    monkeypatch.delenv("CONTEXTLAKE_NO_AUTO_COMPLETION", raising=False).
    """
    monkeypatch.setenv("CONTEXTLAKE_NO_AUTO_COMPLETION", "1")


@pytest.fixture(autouse=True)
def _clean_color_env(monkeypatch):
    """Keep tests hermetic: a shell (or terminal wrapper) that sets FORCE_COLOR
    would make style.ok()/style.warn() emit ANSI codes even under pytest's
    non-tty capsys capture, breaking any test that asserts a literal plain-text
    log/output string. Tests that want to exercise the colored path set
    FORCE_COLOR/NO_COLOR explicitly via monkeypatch.setenv.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make time.sleep a no-op so backoff tests run instantly."""
    monkeypatch.setattr(core.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Clear the kb tier's circuit breakers around every test.

    ``kb.resilience`` keys its breakers by endpoint in a process-wide registry
    (deliberately: connectors and provider clients are rebuilt per source/repo,
    so instance-owned state would never accumulate enough failures to open).
    Left uncleared, a test that trips a breaker would make a later, unrelated
    test's provider call short-circuit instead of reaching its own stub.
    Imported inside the fixture: ``kb`` is an optional extra, and this core-tier
    conftest must still work when it isn't installed.
    """
    def _clear():
        try:
            from contextlake.kb.resilience import reset_breakers
        except ImportError:
            return
        reset_breakers()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _no_leaked_progress_bars():
    """Drop any live progress bar between tests.

    Bars register themselves globally so the log handler can erase/repaint them.
    A test that builds one on a fake TTY and never calls done() would otherwise
    leave it registered, and the next test's log output would try to repaint a
    bar whose scripted clock is exhausted.
    """
    from contextlake import style

    yield
    with style._active_lock:
        style._active.clear()


@pytest.fixture
def base_config():
    """A realistic config dict mirroring DEFAULT_CONFIG values."""
    from contextlake.config import DEFAULT_CONFIG

    return DEFAULT_CONFIG.copy()


def make_local_repo(root, rel_path, branch="main", dirty=False):
    """Create a synthetic local clone with a real .git dir under ``root``."""
    repo = root / rel_path
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    if dirty:
        (repo / "dirty.txt").write_text("uncommitted")
    return repo


@pytest.fixture
def commit_raw_bytes():
    """Commit whose object bytes are written verbatim, undecodable bytes and all.

    DO NOT SIMPLIFY THIS TO ``git commit -F <file>`` OR ``GIT_AUTHOR_NAME=...``.
    It looks like an elaborate way to spell an ordinary commit and it is not. Doing
    so silently disarms all three of its callers -- they keep passing, against code
    with the bug back in it:

    * ``tests/kb/test_kb_references.py``   (``git log --format=%s``)
    * ``tests/kb/test_kb_symbol_refs.py``  (``git blame --line-porcelain``)
    * ``tests/kb/test_kb_ownership.py``    (``git log --format=%an``)

    The reason is that ``git commit`` **transcodes**. Handed a message or an author
    ident that is not valid UTF-8, it converts to UTF-8 before writing the object,
    so the byte you carefully placed never reaches the decoder under test. Measured,
    not assumed: committing a raw ``0x96`` the ordinary way stores ``c2 96`` (the
    UTF-8 encoding of U+0096), which decodes cleanly and proves nothing. Verify with
    ``git cat-file -p HEAD | xxd`` if you doubt it -- and note that the very first
    version of these three tests passed against the *unfixed* code for exactly this
    reason.

    ``hash-object`` is the only route that bypasses the transcode, and it is
    faithful rather than contrived: what lands in the object database is byte for
    byte what a real repository holds. Those are not rare. History written by older
    Windows tooling carries cp1252 bytes (0x96 is its en-dash) with no ``encoding``
    header to declare them, and git replays them exactly as stored -- which is how
    one commit killed an indexing run across a 20-repository fleet.
    """
    import subprocess

    def _commit(repo, *, message: bytes,
                author: bytes = b"T Ester <t@example.com>",
                when: str = "1785000000 +0000") -> str:
        def g(*args, **kw):
            return subprocess.run(["git", "-C", str(repo), *args],
                                  check=True, capture_output=True, **kw)

        tree = g("write-tree").stdout.strip().decode()
        parent = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "-q", "--verify", "HEAD"],
            capture_output=True).stdout.strip().decode()
        header = f"tree {tree}\n".encode()
        if parent:
            header += f"parent {parent}\n".encode()
        obj = (header
               + b"author " + author + b" " + when.encode() + b"\n"
               + b"committer " + author + b" " + when.encode() + b"\n\n"
               + message)
        sha = g("hash-object", "-t", "commit", "-w", "--stdin",
                input=obj).stdout.strip().decode()
        g("update-ref", "HEAD", sha)
        return sha

    return _commit
