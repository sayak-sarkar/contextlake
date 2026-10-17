"""Which clone readers follow a symlink out of the clone, and which stay inside it.

contextlake reads data wherever a configured source points (owner, 2026-10-15), so
``get_readme`` on stdio, for the shared token or for a key with no enforced axis, and the
live dashboard's README panel follow a link out of the clone. Four readers stay inside the
clone:

- ``get_readme`` for a limited key (owner decision 17, 2026-10-17). A ``README.md`` linked to
  ``/proc/self/environ`` returned the server's environment, and the shared token in it, to
  a key scoped to one repository. A key limited only on tools, rate, burst, cost budget,
  owners or external has no repo scope and read the same file until the check asked about
  every enforced axis.
- the wiki README excerpt. The page is stored and served to every key that may read the
  repo, and it is written with no caller in hand to check.
- the wiki steering file (``.contextlake/wiki.toml``, owner decision 16, 2026-10-17). Its
  notes go into the same stored page, so a file or parent directory linked out of the clone
  is not read. A link that stays inside the clone is read.
- the README panel of a ``kb dashboard --site`` export (decision 16). The export is made to
  be shared.

The marker is an invented string in a scratch file. Each refusal has a positive control: a
guard that refused everything would pass the refusal half alone.
"""

import asyncio
import os

import pytest
from mcp import Client

from contextlake.kb import server as server_mod
from contextlake.kb.dashboard.data import _readme_html
from contextlake.kb.grants import GrantCheck
from contextlake.kb.model import Repo
from contextlake.kb.paths import read_repo_file
from contextlake.kb.scoped_store import ScopedStore
from contextlake.kb.server import SHARED_TOKEN_KEY_ID, Principal, build_server
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


# The store each kind of caller is served. Three shapes:
#
# - `None` is stdio, which gets the bare store.
# - A function is a repo scope handed straight to `ScopedStore`. `None` from it is a scope
#   that could not be resolved: the proxy fails closed, so `get_repo` returns nothing to it
#   and it is refused before the README is read; that case pins the refusal, not the
#   containment.
# - A `(principal, key records)` pair is a key server as `build_http_app` builds one: a
#   `GrantCheck` over the records, and a `ScopedStore` whose scope it resolves from the
#   principal. The records are policy blocks, so these cases reach the README with the
#   same grant data a live key carries.
_KEY = Principal("k_test")
CALLERS = {
    "stdio": None,
    "unscoped-key": lambda: ([], False),
    "scoped-key": lambda: (["r"], False),
    "unresolved-scope": lambda: None,
    "tools-only-key": (_KEY, {_KEY.key_id: {"tools": "docs"}}),
    "rate-only-key": (_KEY, {_KEY.key_id: {"rate": "60/min"}}),
    "owners-only-key": (_KEY, {_KEY.key_id: {"owners": "real"}}),
    "cost-budget-only-key": (_KEY, {_KEY.key_id: {"cost_budget": "30s/min"}}),
    "external-only-key": (_KEY, {_KEY.key_id: {"external": True}}),
    "no-axis-key": (_KEY, {_KEY.key_id: {}}),
    "burst-without-rate-key": (_KEY, {_KEY.key_id: {"burst": "20"}}),
    "shared-token": (Principal(SHARED_TOKEN_KEY_ID), {}),
}
FOLLOWS = {
    "stdio": True, "unscoped-key": True, "scoped-key": False, "unresolved-scope": False,
    "tools-only-key": False, "rate-only-key": False, "owners-only-key": False,
    "cost-budget-only-key": False, "external-only-key": False,
    "no-axis-key": True,
    # A burst with no rate binds nothing (`enforced_axes`), so the key limits nothing.
    "burst-without-rate-key": True,
    "shared-token": True,
}


class _Ring:
    """The one method `GrantCheck` reads off a keyring."""

    def __init__(self, records):
        self._records = records

    def policy_for(self, key_id):
        return self._records.get(key_id)


def _served(s, caller, monkeypatch):
    who = CALLERS[caller]
    if who is None:
        return build_server(s)
    if callable(who):
        return build_server(ScopedStore(s, who))
    principal, records = who
    monkeypatch.setattr(server_mod, "current_principal", lambda: principal)
    grants = GrantCheck(_Ring(records))
    scoped = ScopedStore(s, lambda: grants.repo_scope(server_mod.current_principal()))
    return build_server(scoped, networked=True, grant_source=grants)


def test_every_caller_has_a_verdict():
    assert set(CALLERS) == set(FOLLOWS)


@pytest.mark.parametrize("caller", list(CALLERS))
def test_mcp_get_readme_follows_a_link_out_only_for_a_full_access_caller(world, caller,
                                                                        monkeypatch):
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    try:
        out = _get_readme(_served(s, caller, monkeypatch))
    finally:
        s.close()
    if FOLLOWS[caller]:
        assert out["found"] is True and SECRET in out["markdown"]
    else:
        assert out["found"] is False and SECRET not in out["markdown"]


@pytest.mark.parametrize("caller", list(CALLERS))
def test_mcp_get_readme_reads_a_normal_readme_for_every_caller(world, caller, monkeypatch):
    """The positive control for the refusals above: a limited key still gets a real README.

    An unresolved scope is refused before the file is read, so it has nothing to read."""
    tmp, clone, _ = world
    (clone / "README.md").write_text("# Svc\nDoes the thing.\n")
    s = _store(tmp, clone)
    try:
        out = _get_readme(_served(s, caller, monkeypatch))
    finally:
        s.close()
    if caller == "unresolved-scope":
        assert out["found"] is False
        return
    assert out["found"] is True and out["path"] == "README.md"
    assert "Does the thing" in out["markdown"]


@pytest.mark.parametrize("caller", ["scoped-key", "tools-only-key", "rate-only-key"])
def test_mcp_get_readme_reads_a_symlink_that_stays_inside_for_a_limited_key(
        world, caller, monkeypatch):
    tmp, clone, _ = world
    (clone / "docs").mkdir()
    (clone / "docs" / "intro.md").write_text("# Inside\n")
    (clone / "README.md").symlink_to(clone / "docs" / "intro.md")
    s = _store(tmp, clone)
    try:
        out = _get_readme(_served(s, caller, monkeypatch))
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


def test_dashboard_readme_html_contained_refuses_a_symlink_out_of_the_clone(world):
    """The static export's switch. A link out is refused and a real README still renders."""
    tmp, clone, outside = world
    (clone / "README.md").symlink_to(outside / "secret.txt")
    s = _store(tmp, clone)
    try:
        assert _readme_html(s, "r", contained=True) is None
        (clone / "README.md").unlink()
        (clone / "README.md").write_text("# Widget\nRun make test.\n")
        assert "make test" in _readme_html(s, "r", contained=True)
    finally:
        s.close()


def test_wiki_steering_refuses_a_symlinked_file_out_of_the_clone(world):
    _, clone, outside = world
    (clone / ".contextlake").mkdir()
    (clone / ".contextlake" / "wiki.toml").symlink_to(outside / "wiki.toml")
    # Precondition: the link is real and follows through, so a refusal is the guard's doing.
    assert SECRET in (clone / ".contextlake" / "wiki.toml").read_text()
    out = read_wiki_steering(clone)
    assert out == {"notes": [], "pages": [], "unreadable": False}


def test_wiki_steering_refuses_a_symlinked_parent_directory_out_of_the_clone(world):
    _, clone, outside = world
    (clone / ".contextlake").symlink_to(outside, target_is_directory=True)
    assert SECRET in (clone / ".contextlake" / "wiki.toml").read_text()
    out = read_wiki_steering(clone)
    assert out == {"notes": [], "pages": [], "unreadable": False}


def test_wiki_steering_reads_a_symlinked_file_that_stays_inside_the_clone(world):
    """The positive control: contained refuses a link out, not every link."""
    _, clone, _ = world
    (clone / ".contextlake").mkdir()
    (clone / "docs").mkdir()
    (clone / "docs" / "steer.toml").write_text('notes = "kept inside"\npages = ["api"]\n')
    (clone / ".contextlake" / "wiki.toml").symlink_to(clone / "docs" / "steer.toml")
    assert read_wiki_steering(clone)["notes"] == ["kept inside"]
    assert read_wiki_steering(clone)["pages"] == ["api"]


def test_wiki_steering_reads_a_plain_file(world):
    _, clone, _ = world
    (clone / ".contextlake").mkdir()
    (clone / ".contextlake" / "wiki.toml").write_text('notes = "plain"\n')
    assert read_wiki_steering(clone)["notes"] == ["plain"]


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
