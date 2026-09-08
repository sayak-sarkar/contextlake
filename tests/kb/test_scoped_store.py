"""The store-layer repo filter.

Every assertion here is about what a caller RECEIVES, not about what a predicate
returns: the predicate has its own tests in `test_scope.py`, and a correct predicate
wired to nothing is the write-with-no-consumer shape this frame exists to keep out.
"""

from __future__ import annotations

import pytest

from contextlake.kb.model import EXTERNAL_REPO, SHARED_REPO, SYSTEM_REPO
from contextlake.kb.scoped_store import (
    ScopedStore,
    open_request_scope,
    reset_request_scope,
)
from contextlake.kb.store.base import Stats


class _Node:
    def __init__(self, node_id, repo, name="n"):
        self.id = node_id
        self.repo = repo
        self.name = name


class _Edge:
    def __init__(self, src, dst, relation="calls"):
        self.src = src
        self.dst = dst
        self.relation = relation


class _Repo:
    def __init__(self, rid):
        self.id = rid
        self.path = f"/repos/{rid}"


class _FakeStore:
    """Two repositories, a shared node, and the two off-by-default sentinels."""

    path = "/store"

    def __init__(self):
        self.nodes = {
            "mine": _Node("mine", "acme/api"),
            "theirs": _Node("theirs", "other/api"),
            "shared": _Node("shared", SHARED_REPO),
            "ext": _Node("ext", EXTERNAL_REPO),
        }
        self.closed = False

    # -- reads the proxy forwards -------------------------------------------
    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def neighbors(self, node_id, relation=None, direction="both"):
        # The concrete leak case: a shared module node both repos import.
        return [_Edge("shared", "mine"), _Edge("shared", "theirs")]

    def search(self, query, kind=None, repo=None, limit=20):
        return list(self.nodes.values())

    def nodes_by_name(self, name, kind=None, repo=None):
        return list(self.nodes.values())

    def list_repos(self):
        return [_Repo("acme/api"), _Repo("other/api")]

    def list_partitions(self):
        return ["acme/api", "@wiki:acme/api", "@wiki:acme/api::core",
                "other/api", "@wiki:other/api",
                SHARED_REPO, EXTERNAL_REPO, SYSTEM_REPO]

    def repo_counts(self, repo_id):
        return (10, 5) if repo_id in ("acme/api", "other/api") else (1, 0)

    def get_repo(self, repo_id):
        return _Repo(repo_id)

    def get_repo_parser_version(self, repo_id):
        return "12"

    def get_repo_indexed_at(self, repo_id):
        return "2026-09-08T00:00:00Z"

    def stats(self):
        return Stats(repos=2, nodes=99, edges=42, by_confidence={"EXTRACTED": 99})

    def close(self):
        self.closed = True


@pytest.fixture
def request_scope():
    token = open_request_scope()
    yield
    reset_request_scope(token)


def _scoped(patterns=("acme/**",), external=False, store=None):
    return ScopedStore(store or _FakeStore(), lambda: (list(patterns), external))


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------

def test_a_node_outside_the_scope_is_not_returned(request_scope):
    s = _scoped()
    assert s.get_node("mine") is not None
    assert s.get_node("theirs") is None


def test_list_repos_shows_only_granted_repositories(request_scope):
    assert [r.id for r in _scoped().list_repos()] == ["acme/api"]


def test_search_drops_denied_rows(request_scope):
    got = {n.id for n in _scoped().search("anything")}
    assert "mine" in got
    assert "theirs" not in got


def test_a_denied_repo_argument_returns_empty_rather_than_filtering_after(
        request_scope):
    """A denied `repo=` is refused outright, not passed through and filtered.

    Filtering afterwards returns a normal empty answer, indistinguishable from a
    repository that exists and simply has no match -- and the note-building code
    above these calls then echoes the denied repo id back in prose, which is a
    content-bearing existence oracle rather than a timing residual.
    """
    assert _scoped().search("anything", repo="other/api") == []
    assert _scoped().nodes_by_name("n", repo="other/api") == []


def test_get_repo_and_its_metadata_are_all_gated(request_scope):
    s = _scoped()
    assert s.get_repo("other/api") is None
    assert s.get_repo_parser_version("other/api") is None
    assert s.get_repo_indexed_at("other/api") is None
    assert s.repo_counts("other/api") == (0, 0)
    # The positive control: the granted repo answers on every one of them, so the
    # four refusals above are the scope and not a proxy that returns None always.
    assert s.get_repo("acme/api") is not None
    assert s.get_repo_parser_version("acme/api") == "12"
    assert s.get_repo_indexed_at("acme/api") is not None
    assert s.repo_counts("acme/api") == (10, 5)


# --------------------------------------------------------------------------
# The both-endpoints rule
# --------------------------------------------------------------------------

def test_an_edge_to_a_denied_repo_does_not_come_back(request_scope):
    """The leak this rule exists to stop, in its concrete form.

    A `(shared)` module node is visible to every key, and both repositories import
    it. Asking for its neighbours with only the QUERIED node filtered returns the
    edge to the denied repository's file, and a file node's id is that repository's
    id followed by a path inside it. The caller was never granted that repository
    and now has its name and a path within it.
    """
    edges = _scoped().neighbors("shared")
    assert [e.dst for e in edges] == ["mine"]
    assert "theirs" not in [e.dst for e in edges]


def test_an_unscoped_caller_still_sees_both_edges(request_scope):
    """The positive control for the rule above: the filter is the scope, not a
    proxy that drops the second edge unconditionally."""
    assert len(_scoped(patterns=()).neighbors("shared")) == 2


# --------------------------------------------------------------------------
# Sentinels and the external axis
# --------------------------------------------------------------------------

def test_the_off_by_default_sentinels_are_not_visible(request_scope):
    visible = _scoped().visible_partitions()
    assert SHARED_REPO in visible          # or imports break
    assert EXTERNAL_REPO not in visible    # its own axis, off
    assert SYSTEM_REPO not in visible      # discloses an unindexed call target


def test_external_grants_only_the_external_sentinel(request_scope):
    visible = _scoped(external=True).visible_partitions()
    assert EXTERNAL_REPO in visible
    # It is not a wildcard: `(system)` still needs `**`.
    assert SYSTEM_REPO not in visible
    assert "other/api" not in visible


def test_the_wildcard_reaches_the_system_sentinel(request_scope):
    assert SYSTEM_REPO in _scoped(patterns=("**",)).visible_partitions()


def test_the_open_ended_wiki_family_is_in_scope(request_scope):
    """The reason this design exists: an `IN (?,...)` list that still covers a
    family whose members cannot be enumerated from the grant."""
    visible = _scoped().visible_partitions()
    assert "@wiki:acme/api" in visible
    assert "@wiki:acme/api::core" in visible
    assert "@wiki:other/api" not in visible


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def test_stats_is_recomputed_over_the_visible_partitions(request_scope):
    """Forwarding the real `stats()` would tell a key scoped to one repository how
    large the whole fleet is, which is the same fact `list_repos` is filtered to
    hide."""
    real = _FakeStore().stats()
    scoped = _scoped().stats()
    assert real.nodes == 99 and scoped.nodes != 99
    assert scoped.repos == 1
    # `by_confidence` is store-wide with no per-partition version to sum, so it is
    # dropped rather than forwarded beside two scoped numbers.
    assert scoped.by_confidence == {}


def test_an_unscoped_caller_gets_the_real_stats(request_scope):
    assert _scoped(patterns=()).stats().nodes == 99


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------

def test_the_proxy_has_no_getattr_passthrough():
    """A delegating proxy forwards whatever it is asked for, so the first attribute
    nobody thought about is served unfiltered and the test that would catch it has
    to predict the attribute's name."""
    assert "__getattr__" not in ScopedStore.__dict__


def test_path_is_forwarded():
    """Five tool bodies read `getattr(store, "path", None)` and each has a pathless
    fallback, so omitting it does not raise: it silently takes the wrong branch and
    answers `found=False`. The path is the store's own, not per-repo."""
    assert _scoped().path == "/store"


def test_conn_is_still_forwarded_and_that_is_deliberate():
    """PINS A KNOWN GAP so it cannot be mistaken for a finished rule.

    Hiding `.conn` breaks `repo_dependencies`, `repo_flow` and `repo_event_flow`,
    which reach raw SQL through `arch/resolve.py`. They are filtered at their tool
    bodies instead. This assertion is here so that whoever hides `conn` has to
    delete it deliberately, having done the `arch/resolve.py` rewrite first.
    """
    assert "conn" in ScopedStore.__dict__, (
        "`conn` was removed from ScopedStore. That is the right end state, but it "
        "breaks repo_dependencies, repo_flow and repo_event_flow unless "
        "arch/resolve.py was rewritten off raw SQL first -- do that, then delete "
        "this test rather than weakening it")


def test_a_direct_construction_with_no_principal_denies_everything(request_scope):
    """Unreachable through `guarded`, which raises `IdentityUnset` above the anchor.

    Kept and tested by calling the proxy DIRECTLY, because a filter that answers
    "everything" for nobody is the one bug this class must not be able to have, and
    driving it through a tool call would never reach the branch.
    """
    s = ScopedStore(_FakeStore(), lambda: None)
    assert s.get_node("mine") is None
    assert s.list_repos() == []
    assert s.search("anything") == []
    assert s.visible_partitions() == []


def test_close_reaches_the_real_store():
    real = _FakeStore()
    ScopedStore(real, lambda: ([], False)).close()
    assert real.closed is True


# --------------------------------------------------------------------------
# The disk readers, which the proxy cannot see
# --------------------------------------------------------------------------

def test_every_tool_that_reads_the_store_path_is_gated():
    """A tool that opens a file by path bypasses every forwarded store method.

    THIS IS A COMPLETENESS TEST, and it exists because unit tests could not see the
    defect it guards. `get_repo_brief` returned the denied repository's own brief
    with `found=true` to a scoped key, while `search_code` on the same key was
    correctly filtered -- the difference being that one went through a covered
    method and the other resolved `store.path` and opened a file. Found by driving
    a real HTTP server on 2026-09-08, not by the suite.

    Read from the SOURCE rather than from a list, so a sixth tool that reads
    `store.path` and forgets the gate turns this red instead of shipping unfiltered.
    """
    import inspect
    import re

    from contextlake.kb import server

    src = inspect.getsource(server.build_server)
    # Split into tool bodies: `def <name>(` at the tool indent inside build_server.
    bodies = re.split(r"\n    def ", src)
    offenders = []
    for body in bodies:
        name = body.split("(", 1)[0].strip()
        if 'getattr(store, "path", None)' not in body:
            continue
        if "_may_read_repo(" in body or "_fleet_readable(" in body:
            continue
        if "SCOPE-EXEMPT:" in body:
            # An exemption has to SAY why, in the body, where the next reader of that
            # tool sees it. `graph_health` is the one: it is scoped through
            # `store.list_repos()` rather than by a gate, and gating it would refuse
            # a tool that already answers correctly for a scoped key.
            continue
        offenders.append(name)
    assert offenders == [], (
        f"these tools read `store.path` and open a file without asking whether the "
        f"caller may read it, so a scoped key receives content from repositories it "
        f"was never granted: {offenders}. Add `_may_read_repo(repo)` for a per-repo "
        f"tool, or `_fleet_readable()` for a fleet-wide one.")


def test_the_gate_helpers_are_inert_without_a_scoped_store():
    """stdio and the dashboard hand `build_server` a bare `Store`.

    A bare store has no `allows_repo`/`is_scoped`, and the helpers must read that as
    "no scope applies" rather than raising or denying. Local-first property P4: the
    local path answers exactly what it answered before.
    """
    class _Bare:
        path = "/store"

    assert getattr(_Bare(), "allows_repo", None) is None
    assert getattr(_Bare(), "is_scoped", None) is None


def test_allows_repo_and_is_scoped_answer_for_the_proxy(request_scope):
    s = _scoped()
    assert s.allows_repo("acme/api") is True
    assert s.allows_repo("other/api") is False
    assert s.is_scoped() is True
    # An unscoped principal reads every repo and is not "scoped", so the fleet-wide
    # documents stay available to it.
    assert _scoped(patterns=()).is_scoped() is False
    # And no principal at all is treated as scoped, so the fleet readers refuse it.
    assert ScopedStore(_FakeStore(), lambda: None).is_scoped() is True
