"""Shared pytest fixtures for gitlab_sync.

The tool shells out to ``git`` and ``glab``; tests must never touch the network
or a real GitLab. ``fake_subprocess`` swaps ``core.subprocess.run`` for a
programmable stub, and ``no_sleep`` makes retry/backoff instant.
"""

import logging
import subprocess
import types

import pytest

from gitlab_sync import core


@pytest.fixture
def gls_logs(caplog):
    """Capture the gitlab_sync logger's records regardless of propagation.

    The package logger sets propagate=False, so caplog's root handler misses it;
    attaching caplog's handler directly to the logger captures reliably.
    """
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("gitlab_sync")
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
    logging.getLogger("gitlab_sync").handlers.clear()
    yield
    logging.getLogger("gitlab_sync").handlers.clear()


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


@pytest.fixture
def no_sleep(monkeypatch):
    """Make time.sleep a no-op so backoff tests run instantly."""
    monkeypatch.setattr(core.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def base_config():
    """A realistic config dict mirroring DEFAULT_CONFIG values."""
    from gitlab_sync.config import DEFAULT_CONFIG

    return DEFAULT_CONFIG.copy()


def make_local_repo(root, rel_path, branch="main", dirty=False):
    """Create a synthetic local clone with a real .git dir under ``root``."""
    repo = root / rel_path
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    if dirty:
        (repo / "dirty.txt").write_text("uncommitted")
    return repo
