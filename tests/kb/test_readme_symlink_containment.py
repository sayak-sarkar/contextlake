"""A clone's README and steering readers follow a symlink, except for a scoped caller.

contextlake reads data wherever a configured source points (owner, 2026-10-15), so
``get_readme`` on stdio or for an unscoped key, the dashboard's README panel and the wiki
steering file follow a link out of the clone. Two readers stay inside the clone:

- ``get_readme`` for a scoped key. A ``README.md`` linked to ``/proc/self/environ`` returned
  the server's environment, and the shared token in it, to a key scoped to one repository.
- the wiki README excerpt. The page is stored and served to every key that may read the
  repo, and it is written with no caller in hand to check.

The marker is an invented string in a scratch file. Each refusal has a positive control: a
guard that refused everything would pass the refusal half alone.
"""

import asyncio
import os

import pytest
from mcp import Client

from contextlake.kb.dashboard.data import _readme_html
from contextlake.kb.model import Repo
from contextlake.kb.paths import read_repo_file
from contextlake.kb.scoped_store import ScopedStore
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.generate import _readme_excerpt
from contextlake.kb.wiki.steering import read_wiki_steering

SECRET = "TOKEN-MARKER-7f3a91-not-a-real-credential"
NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")


@pytest.fixture
def world(tmp_path):
    """``clone/`` (the indexed repo) beside ``outside/`` (a file only a link reaches)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET)
    (outside / "wiki.toml").write_text(f'notes = "{SECRET}"\npages = ["x"]\n')
    return tmp_path, clone, outside


# --- the helper -----------------------------------------------------------------------

@pytest.mark.parametrize("contained", [True, False])
def test_a_normal_readme_reads(world, contained):
    _, clone, _ = world
    (clone / "README.md").write_text("# Hello\n")
    assert read_repo_file(clone, NAMES, contained=contained) == ("README.md", "# Hello\n")


def test_a_symlink_pointing_outside_the_clone_is_followed(world):
    _, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    assert read_repo_file(clone, NAMES, contained=False) == ("README.md", SECRET)


def test_contained_refuses_a_symlink_pointing_outside_the_clone(world):
    _, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    # Precondition: the link is real and follows through, so a refusal is the guard's doing.
    assert (clone / "README.md").read_text() == SECRET
    assert read_repo_file(clone, NAMES, contained=True) is None


@pytest.mark.parametrize("contained", [True, False])
def test_a_symlink_that_stays_inside_the_clone_reads(world, contained):
    _, clone, _ = world
    (clone / "docs").mkdir()
    (clone / "docs" / "intro.md").write_text("# Real\n")
    (clone / "README.md").symlink_to(clone / "docs" / "intro.md")
    assert read_repo_file(clone, NAMES, contained=contained) == ("README.md", "# Real\n")


def test_a_symlinked_parent_directory_is_followed_unless_contained(world):
    _, clone, outside = world
    (outside / "README.md").write_text(SECRET)
    (clone / "sub").symlink_to(outside, target_is_directory=True)
    assert read_repo_file(clone, ["sub/README.md"], contained=False) == ("sub/README.md",
                                                                         SECRET)
    assert read_repo_file(clone, ["sub/README.md"], contained=True) is None


def test_contained_a_link_out_does_not_hide_a_real_readme_after_it(world):
    _, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    (clone / "README.rst").write_text("Real\n")
    assert read_repo_file(clone, NAMES, contained=True) == ("README.rst", "Real\n")


def test_contained_a_clone_reached_through_a_symlink_still_reads(world):
    tmp, clone, _ = world
    (clone / "README.md").write_text("# Hi\n")
    alias = tmp / "alias"
    alias.symlink_to(clone, target_is_directory=True)
    assert read_repo_file(alias, NAMES, contained=True) == ("README.md", "# Hi\n")


def test_contained_a_refusal_is_logged(world, gls_logs):
    _, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    read_repo_file(clone, NAMES, contained=True)
    assert gls_logs.records, "capture is empty: the logger did not reach the fixture"
    assert any("resolves outside the clone" in r.getMessage() for r in gls_logs.records)


@pytest.mark.skipif(not os.path.exists("/dev/null") or not hasattr(os, "mkfifo"),
                    reason="needs /dev/null and FIFOs")
@pytest.mark.parametrize("target", ["device", "fifo"])
def test_a_link_to_a_device_or_fifo_is_skipped_not_read(world, target):
    """A followed link can name ``/dev/zero``, which reads until memory runs out, or a FIFO,
    which blocks. Only a regular file is read, and the next name is still tried. The test
    links to ``/dev/null``, the same file type: a regression fails here instead of exhausting
    memory."""
    tmp, clone, _ = world
    if target == "fifo":
        os.mkfifo(tmp / "the.fifo")
        (clone / "README.md").symlink_to(tmp / "the.fifo")
    else:
        (clone / "README.md").symlink_to("/dev/null")
    (clone / "README.rst").write_text("Real\n")
    assert read_repo_file(clone, NAMES, contained=False) == ("README.rst", "Real\n")


# --- get_readme over MCP --------------------------------------------------------------

def _store(tmp_path, clone):
    s = SqliteStore(tmp_path / "k.sqlite")
    s.upsert_repo(Repo(id="r", path=str(clone)))
    return s


def _get_readme(srv):
    async def go():
        async with Client(srv) as client:
            return await client.call_tool("get_readme", {"repo": "r"})
    out = asyncio.run(go()).structured_content
    if isinstance(out, dict) and set(out) == {"result"}:
        out = out["result"]
    return out


# The store each kind of caller is served: stdio and a token-only server get the bare
# store; a key server wraps it in `ScopedStore` with the key's scope. `None` is a key whose
# scope could not be resolved. The proxy fails closed, so `get_repo` returns nothing to it
# and it is refused before the README is read; the case pins that, not the containment.
CALLERS = {
    "stdio": None,
    "unscoped-key": lambda: ([], False),
    "scoped-key": lambda: (["r"], False),
    "unresolved-scope": lambda: None,
}
FOLLOWS = {"stdio": True, "unscoped-key": True, "scoped-key": False, "unresolved-scope": False}


def _served(s, caller):
    scope = CALLERS[caller]
    return build_server(s if scope is None else ScopedStore(s, scope))


@pytest.mark.parametrize("caller", list(CALLERS))
def test_mcp_get_readme_follows_a_link_out_only_for_an_unscoped_caller(world, caller):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    try:
        out = _get_readme(_served(s, caller))
    finally:
        s.close()
    if FOLLOWS[caller]:
        assert out["found"] is True and SECRET in out["markdown"]
    else:
        assert out["found"] is False and SECRET not in out["markdown"]


@pytest.mark.parametrize("caller", ["stdio", "unscoped-key", "scoped-key"])
def test_mcp_get_readme_reads_a_normal_readme_for_every_caller(world, caller):
    """The positive control for the refusals above: a scoped key still gets a real README."""
    tmp, clone, _ = world
    (clone / "README.md").write_text("# Svc\nDoes the thing.\n")
    s = _store(tmp, clone)
    try:
        out = _get_readme(_served(s, caller))
    finally:
        s.close()
    assert out["found"] is True and out["path"] == "README.md"
    assert "Does the thing" in out["markdown"]


def test_mcp_get_readme_reads_a_symlink_that_stays_inside_for_a_scoped_key(world):
    tmp, clone, _ = world
    (clone / "docs").mkdir()
    (clone / "docs" / "intro.md").write_text("# Inside\n")
    (clone / "README.md").symlink_to(clone / "docs" / "intro.md")
    s = _store(tmp, clone)
    try:
        out = _get_readme(_served(s, "scoped-key"))
    finally:
        s.close()
    assert out["found"] is True and "Inside" in out["markdown"]


def test_mcp_get_readme_does_not_read_a_link_to_a_directory(world):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside)  # a directory, not a file
    s = _store(tmp, clone)
    try:
        out = _get_readme(build_server(s))
    finally:
        s.close()
    assert out["found"] is False


# --- the other readers ----------------------------------------------------------------

def test_wiki_readme_excerpt_refuses_a_symlink_out_of_the_clone(world):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    assert _readme_excerpt(s, "r") is None
    (clone / "README.md").unlink()
    (clone / "README.md").write_text("# Widget\nRun make test.\n")
    assert "make test" in _readme_excerpt(s, "r")
    s.close()


def test_dashboard_readme_html_follows_a_symlink_out_of_the_clone(world):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    try:
        assert SECRET in _readme_html(s, "r")
    finally:
        s.close()


def test_wiki_steering_follows_a_symlinked_file_out_of_the_clone(world):
    _, clone, outside = world
    (clone / ".contextlake").mkdir()
    (clone / ".contextlake" / "wiki.toml").symlink_to(outside / "wiki.toml")
    assert read_wiki_steering(clone)["notes"] == [SECRET]


def test_wiki_steering_follows_a_symlinked_parent_directory(world):
    _, clone, outside = world
    (clone / ".contextlake").symlink_to(outside, target_is_directory=True)
    assert read_wiki_steering(clone)["notes"] == [SECRET]


@pytest.mark.parametrize("contained", [True, False])
@pytest.mark.parametrize("make", [
    pytest.param(lambda c: os.symlink("README.md", c / "README.md"), id="self-loop"),
    pytest.param(lambda c: (os.symlink("b", c / "README.md"),
                            os.symlink("README.md", c / "b")), id="two-link-cycle"),
])
def test_a_symlink_loop_is_not_read_and_does_not_raise(tmp_path, make, contained):
    """A README that is a symlink LOOP answers not-found, never raises.

    Loop handling in `Path.resolve()` has differed across the Python versions CI runs
    (3.10 to 3.12 raise RuntimeError, 3.13 does not), so the cases live here where the CI
    matrix runs them, in both modes.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    make(clone)
    assert read_repo_file(clone, ("README.md",), contained=contained) is None
