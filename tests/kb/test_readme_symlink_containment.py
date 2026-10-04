"""A mirrored clone is untrusted: a symlink committed to it must not read outside it.

Four readers opened a file inside a clone with ``is_file()`` + ``read_text()``, both of which
follow symlinks: ``get_readme`` (MCP), the wiki generator's README excerpt, the dashboard's
README panel, and the wiki steering file. A ``README.md`` linking to a file outside the clone
returned that file. Over MCP the target can be ``/proc/self/environ``, which holds the
server's shared token, so a key scoped to one repository could read a full-scope credential.

The secret here is an invented marker in a scratch file; what is asserted is that the marker
does NOT come back, and that a legitimate README still does (a guard that refused everything
would pass the first half alone).
"""

import asyncio
import os

import pytest
from mcp import Client

from contextlake.kb.dashboard.data import _readme_html
from contextlake.kb.model import Repo
from contextlake.kb.paths import read_repo_file
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.generate import _readme_excerpt
from contextlake.kb.wiki.steering import read_wiki_steering

SECRET = "TOKEN-MARKER-7f3a91-not-a-real-credential"
NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")


@pytest.fixture
def world(tmp_path):
    """``clone/`` (the indexed repo) beside ``outside/`` (a file it must never reach)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET)
    (outside / "wiki.toml").write_text(f'notes = "{SECRET}"\npages = ["x"]\n')
    return tmp_path, clone, outside


# --- the helper -----------------------------------------------------------------------

def test_a_normal_readme_reads(world):
    _, clone, _ = world
    (clone / "README.md").write_text("# Hello\n")
    assert read_repo_file(clone, NAMES) == ("README.md", "# Hello\n")


def test_a_symlink_pointing_outside_the_clone_is_refused(world):
    _, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    # Precondition: the link is real and follows through, so a refusal is the guard's doing.
    assert (clone / "README.md").read_text() == SECRET
    assert read_repo_file(clone, NAMES) is None


def test_a_symlink_that_stays_inside_the_clone_still_reads(world):
    _, clone, _ = world
    (clone / "docs").mkdir()
    (clone / "docs" / "intro.md").write_text("# Real\n")
    (clone / "README.md").symlink_to(clone / "docs" / "intro.md")
    assert read_repo_file(clone, NAMES) == ("README.md", "# Real\n")


def test_a_symlinked_parent_directory_that_escapes_is_refused(world):
    _, clone, outside = world
    (outside / "README.md").write_text(SECRET)
    (clone / "sub").symlink_to(outside, target_is_directory=True)
    assert read_repo_file(clone, ["sub/README.md"]) is None


def test_a_hostile_candidate_does_not_hide_a_real_one_after_it(world):
    _, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    (clone / "README.rst").write_text("Real\n")
    assert read_repo_file(clone, NAMES) == ("README.rst", "Real\n")


def test_a_clone_reached_through_a_symlink_still_reads(world):
    tmp, clone, _ = world
    (clone / "README.md").write_text("# Hi\n")
    alias = tmp / "alias"
    alias.symlink_to(clone, target_is_directory=True)
    assert read_repo_file(alias, NAMES) == ("README.md", "# Hi\n")


def test_a_refusal_is_logged(world, gls_logs):
    _, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    read_repo_file(clone, NAMES)
    assert gls_logs.records, "capture is empty: the logger did not reach the fixture"
    assert any("resolves outside the clone" in r.getMessage() for r in gls_logs.records)


# --- the shipped paths ----------------------------------------------------------------

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


def test_mcp_get_readme_refuses_a_symlink_out_of_the_clone(world):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    out = _get_readme(build_server(s))
    s.close()
    assert out["found"] is False
    assert SECRET not in out["markdown"]


def test_mcp_get_readme_still_reads_a_normal_readme(world):
    tmp, clone, _ = world
    (clone / "README.md").write_text("# Svc\nDoes the thing.\n")
    s = _store(tmp, clone)
    out = _get_readme(build_server(s))
    s.close()
    assert out["found"] is True and out["path"] == "README.md"
    assert "Does the thing" in out["markdown"]


def test_mcp_get_readme_reads_a_symlink_that_stays_inside(world):
    tmp, clone, _ = world
    (clone / "docs").mkdir()
    (clone / "docs" / "intro.md").write_text("# Inside\n")
    (clone / "README.md").symlink_to(clone / "docs" / "intro.md")
    s = _store(tmp, clone)
    out = _get_readme(build_server(s))
    s.close()
    assert out["found"] is True and "Inside" in out["markdown"]


def test_mcp_get_readme_refuses_a_symlinked_directory_that_escapes(world):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside)  # a directory, not a file
    s = _store(tmp, clone)
    out = _get_readme(build_server(s))
    s.close()
    assert out["found"] is False


def test_wiki_readme_excerpt_refuses_a_symlink_out_of_the_clone(world):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    assert _readme_excerpt(s, "r") is None
    (clone / "README.md").unlink()
    (clone / "README.md").write_text("# Widget\nRun make test.\n")
    assert "make test" in _readme_excerpt(s, "r")
    s.close()


def test_dashboard_readme_html_refuses_a_symlink_out_of_the_clone(world):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    assert _readme_html(s, "r") is None
    (clone / "README.md").unlink()
    (clone / "README.md").write_text("# Panel\n")
    assert "Panel" in _readme_html(s, "r")
    s.close()


def test_wiki_steering_refuses_a_symlinked_file_out_of_the_clone(world):
    _, clone, outside = world
    (clone / ".contextlake").mkdir()
    (clone / ".contextlake" / "wiki.toml").symlink_to(outside / "wiki.toml")
    assert read_wiki_steering(clone) == {"notes": [], "pages": [], "unreadable": False}
    (clone / ".contextlake" / "wiki.toml").unlink()
    (clone / ".contextlake" / "wiki.toml").write_text('notes = "real note"\n')
    assert read_wiki_steering(clone)["notes"] == ["real note"]


def test_wiki_steering_refuses_a_symlinked_parent_directory_that_escapes(world):
    _, clone, outside = world
    (clone / ".contextlake").symlink_to(outside, target_is_directory=True)
    # `outside/wiki.toml` exists and parses, so only the guard keeps its notes out.
    assert read_wiki_steering(clone) == {"notes": [], "pages": [], "unreadable": False}


@pytest.mark.parametrize("make", [
    pytest.param(lambda c: os.symlink("README.md", c / "README.md"), id="self-loop"),
    pytest.param(lambda c: (os.symlink("b", c / "README.md"),
                            os.symlink("README.md", c / "b")), id="two-link-cycle"),
])
def test_a_symlink_loop_is_not_read_and_does_not_raise(tmp_path, make):
    """A README that is a symlink LOOP is the hostile input this helper exists for.

    A clone is untrusted: anyone who can commit can commit a loop. The tool must answer
    not-found, not raise. Loop handling in `Path.resolve()` has differed across the
    Python versions CI runs (3.10 to 3.13), and the local check that first confirmed
    this ran on 3.14 only, so the cases live here where the CI matrix runs them.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    make(clone)
    assert read_repo_file(clone, ("README.md",)) is None
