"""Store paths built from a repo id must name one path inside the store, on the write side too.

`forget` was fixed first (it deleted from a raw id). The writer had the same hole. A repo id is
the normalized remote URL of a clone, and a remote URL is whatever the clone's owner wrote:
`https://evil.example/../..` gives `evil.example/../..`, `https://example.com/team/../other`
gives `example.com/team/../other`, and `file:///dir` gives the absolute id `/dir`.

- `history_path` joined the raw id under `history/`. `evil.example/../..` wrote
  `<commit>.json` into the store root.
- `shard_path` appends `.json` to the LAST segment, so `team/../other` passed its escape check
  and resolved to repo `other`'s shard. `write_shard` for the alias replaced it, and
  `read_shard` for the alias returned it, which is a read of another repo's graph by an id
  that an MCP client can send.

`shard_path` and `history_path` now refuse an id with a `.` or `..` segment, and an absolute
id, with ValueError. Every reader turns that into "not found".

Everything here lives in `tmp_path`.
"""

from __future__ import annotations

import pytest

from contextlake.kb.model import Node
from contextlake.kb.paths import has_dot_segment, is_plain_id
from contextlake.kb.store import shards
from contextlake.kb.store.shards import (
    GraphShard,
    archive_shard,
    history_path,
    list_indexed_commits,
    peek_parser_version,
    read_shard,
    read_shard_at,
    read_shard_with_identity,
    reindex_shard,
    resolve_shard,
    shard_path,
    write_shard,
)

HOSTILE = [
    "evil.example/../..",        # history/<host>/../.. : the store root
    "team/../other",             # alias of repo `other`
    "../x",                      # one level up
    "a/./b",                     # `.` segment: alias of `a/b`
    "..",
    "/abs/dir",                  # absolute: replaces the base
]


def _shard(repo, commit="c1", name="f"):
    return GraphShard(repo=repo, head_commit=commit,
                      nodes=[Node(id=f"{repo}:{name}", repo=repo, kind="function",
                                  name=name, file="a.py")], edges=[])


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "a" / "kb"
    d.mkdir(parents=True)
    # An unrelated repo `other` with a shard and a history snapshot.
    write_shard(d, _shard("other", "c0", "real_other"))
    archive_shard(d, _shard("other", "c0", "real_other"))
    (d.parent / "precious.txt").write_text("precious")
    return d


# --- the rule itself ----------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("..", True), (".", True), ("a/../b", True), ("a/./b", True), ("a\\..\\b", True),
    ("team/app", False), ("gitlab.com/acme/api", False), ("my.repo", False),
    ("a/..b", False), ("a/b..", False), ("host:8443/team/app", False),
])
def test_has_dot_segment(name, expected):
    assert has_dot_segment(name) is expected


@pytest.mark.parametrize("repo_id", HOSTILE + ["\\\\server\\share\\x", "\\root", "C:x"])
def test_is_plain_id_refuses_hostile_ids(repo_id):
    assert is_plain_id(repo_id) is False


@pytest.mark.parametrize("repo_id", ["team/app", "gitlab.com/acme/api", "my.repo",
                                     "name@abc123", "@enrich:team/app", "host:8443/team/app"])
def test_is_plain_id_accepts_real_ids(repo_id):
    assert is_plain_id(repo_id) is True


# --- the choke points ----------------------------------------------------------------------

@pytest.mark.parametrize("repo_id", HOSTILE)
def test_shard_path_refuses(store, repo_id):
    with pytest.raises(ValueError):
        shard_path(store, repo_id)


@pytest.mark.parametrize("repo_id", HOSTILE)
def test_history_path_refuses(store, repo_id):
    with pytest.raises(ValueError):
        history_path(store, repo_id, "c1")


@pytest.mark.parametrize("commit", ["../../x", "a/b", "..", ".", "", "a\\b"])
def test_history_path_refuses_a_commit_that_is_a_path(store, commit):
    """`kb query --as-of <commit>` hands a user string to `history_path`."""
    with pytest.raises(ValueError):
        history_path(store, "team/app", commit)


def test_real_ids_still_resolve(store):
    assert shard_path(store, "team/app") == (store / "graph" / "team" / "app.json").resolve()
    assert history_path(store, "team/app", "abc") == store / "history" / "team" / "app" / "abc.json"
    assert history_path(store, "r", "enrich").name == "enrich.json"


# --- the writers ---------------------------------------------------------------------------

def test_write_shard_for_an_alias_id_does_not_replace_the_other_repos_shard(store):
    before = (store / "graph" / "other.json").read_text()
    with pytest.raises(ValueError):
        write_shard(store, _shard("team/../other", "c9", "from_alias"))
    assert (store / "graph" / "other.json").read_text() == before


def test_archive_shard_for_a_dotdot_id_writes_nothing_outside_history(store):
    """`evil.example/../..` used to put `<commit>.json` in the store root."""
    before = sorted(p.name for p in store.iterdir())
    with pytest.raises(ValueError):
        archive_shard(store, _shard("evil.example/../..", "deadbeef"))
    assert sorted(p.name for p in store.iterdir()) == before
    assert not (store / "deadbeef.json").exists()
    assert (store.parent / "precious.txt").read_text() == "precious"


def test_archive_shard_for_an_absolute_id_writes_nothing(store, tmp_path):
    victim = tmp_path / "victim_dir"
    victim.mkdir()
    with pytest.raises(ValueError):
        archive_shard(store, _shard(str(victim), "deadbeef"))
    assert list(victim.iterdir()) == []


def test_a_refused_write_leaves_the_history_of_the_other_repo_alone(store):
    with pytest.raises(ValueError):
        archive_shard(store, _shard("team/../other", "c9", "from_alias"))
    assert sorted(p.name for p in (store / "history" / "other").iterdir()) == ["c0.json"]


# --- the readers: not found, never an exception, never another repo's data ------------------

@pytest.mark.parametrize("repo_id", HOSTILE)
def test_read_shard_is_not_found(store, repo_id):
    assert read_shard(store, repo_id) is None


def test_an_alias_id_cannot_read_the_other_repos_shard(store):
    """`team/../other` used to return repo `other`'s shard: a read of another repo's graph
    by an id an MCP client can send."""
    assert read_shard(store, "other") is not None          # the real id still reads
    assert read_shard(store, "team/../other") is None
    assert read_shard_with_identity(store, "team/../other") == (None, None)
    identity, load = resolve_shard(store, "team/../other")
    assert identity is None and load() == (None, None)
    assert peek_parser_version(store, "team/../other") is None


def test_read_shard_at_is_not_found_for_a_refused_id_or_commit(store):
    assert read_shard_at(store, "other", "c0") is not None
    assert read_shard_at(store, "team/../other", "c0") is None
    assert read_shard_at(store, "other", "../../graph/other") is None


def test_list_indexed_commits_is_empty_for_a_refused_id(store):
    (store / "history" / "team").mkdir()    # `history/team/../other` only resolves if it exists
    assert list_indexed_commits(store, "other") == ["c0"]
    assert list_indexed_commits(store, "team/../other") == []
    assert list_indexed_commits(store, "/abs") == []


def test_reindex_shard_returns_false_for_a_refused_id(store):
    class _Never:
        def __getattr__(self, name):
            raise AssertionError(f"store.{name} must not be touched for a refused id")

    assert reindex_shard(_Never(), store, "team/../other") is False


def test_the_cache_is_not_consulted_for_a_refused_id(store, monkeypatch):
    monkeypatch.setattr(shards, "_cache_get",
                        lambda *a: pytest.fail("a refused id reached the shard cache"))
    assert read_shard(store, "team/../other") is None


def test_the_mcp_repo_brief_is_not_found_for_an_alias_id(store):
    """`get_repo_brief(repo)` over MCP calls `repo_brief`, which reads through `resolve_shard`.
    The real id answers. The alias, which used to answer with repo `other`'s numbers, is
    not found, and nothing raises to the client."""
    from contextlake.kb.wiki.generate import repo_brief

    assert repo_brief(store, "other") is not None
    assert repo_brief(store, "team/../other") is None
    assert repo_brief(store, "/abs/dir") is None
