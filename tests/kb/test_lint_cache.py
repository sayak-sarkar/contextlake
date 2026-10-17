"""`lint_result` walks every edge in every shard, and the dashboard calls it per request.

Measured during the investigation behind this change: on a large store the health panel
spent seconds re-parsing shards and re-resolving every edge endpoint, on every refresh of
a page nothing had changed behind. The walk is now cached on the same
`(path, mtime_ns, size)` identity the shard cache already uses.

The tests that matter are not "does it cache" -- they are the three ways a cache like this
goes wrong: it keeps serving an answer after the data changed, it caches a run it could not
fully observe, or it is not actually caching anything and the whole thing is decoration.
"""

import json
import os
import shutil
import subprocess
from datetime import date

from contextlake.kb.cmds import lint as lint_mod
from contextlake.kb.cmds.lint import lint_result
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore


def _edge(src, dst):
    return Edge(src=src, dst=dst, relation="calls", confidence=Confidence.EXTRACTED,
                provenance=Provenance(source_file="a.py", source_line=1,
                                      verified_at=date(2026, 8, 13)))


def _store(tmp_path, *, dangling=False):
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "checkout")))
    nodes = [Node(id="a", repo="r", kind="function", name="a", file="a.py", line_start=1),
             Node(id="b", repo="r", kind="function", name="b", file="b.py", line_start=1)]
    edges = [_edge("a", "ghost" if dangling else "b")]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h", nodes=nodes, edges=edges))
    store.upsert_nodes("r", nodes)
    store.upsert_edges("r", edges)
    return store


def _count_walks(store, monkeypatch):
    """Count `get_node` calls -- the edge walk, which is what actually repeats.

    The first version of this counted shard *parses* and was worthless: the shard LRU in
    `store.shards` already serves a small shard from memory, so the second call skipped the
    parse whether or not this cache existed, and the test passed with the cache disabled.
    The endpoint resolution is the work that runs every time regardless, so that is what a
    cache has to eliminate and what this has to count."""
    calls = {"n": 0}
    real = store.get_node

    def spy(node_id):
        calls["n"] += 1
        return real(node_id)

    monkeypatch.setattr(store, "get_node", spy)
    return calls


def test_a_second_call_does_no_shard_work(tmp_path, monkeypatch):
    """THE LOAD-BEARING ASSERTION. Without the cache both calls parse the shard, so this
    fails; with it the second call is stats only."""
    lint_mod._lint_cache.clear()
    store = _store(tmp_path)
    calls = _count_walks(store, monkeypatch)

    first = lint_result(store, tmp_path)
    after_first = calls["n"]
    second = lint_result(store, tmp_path)

    assert after_first >= 1, "the first call must actually walk the edges"
    assert calls["n"] == after_first, (
        f"the second call re-walked the edges ({calls['n'] - after_first} more get_node "
        "calls): the cache is not being consulted")
    assert first == second


def test_a_rewritten_shard_is_not_served_from_cache(tmp_path, monkeypatch):
    """The failure that would matter most: health that keeps saying 'clean' after the
    graph changed underneath it. Rewrites the shard with a genuinely dangling edge and
    asserts the reported count moves."""
    lint_mod._lint_cache.clear()
    store = _store(tmp_path)
    assert lint_result(store, tmp_path)["dangling"] == 0

    nodes = [Node(id="a", repo="r", kind="function", name="a", file="a.py", line_start=1)]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h", nodes=nodes,
                                     edges=[_edge("a", "ghost")]))
    store.upsert_nodes("r", nodes)

    assert lint_result(store, tmp_path)["dangling"] == 1, (
        "a rewritten shard was answered from cache: the fingerprint is not seeing the "
        "rewrite, or the invalidator is not firing")


def test_a_store_whose_shard_cannot_be_read_is_never_cached(tmp_path, monkeypatch):
    """A run that could not observe every input must not be remembered as if it had.

    Encoding "I could not see this" as part of a fingerprint makes the cache confident
    about a store it never actually read -- the exact collapse this codebase keeps
    finding elsewhere, so it gets its own test rather than a comment."""
    lint_mod._lint_cache.clear()
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="noshard", path=str(tmp_path / "checkout")))

    lint_result(store, tmp_path)
    assert not lint_mod._lint_cache, (
        "a store with an unreadable shard was cached; absence is not an observation")


def test_the_answer_survives_a_caller_mutating_it(tmp_path):
    """The dict is handed to callers that serialise and sometimes decorate it. A cache
    that returns its own object lets one caller's edit become the next caller's data."""
    lint_mod._lint_cache.clear()
    store = _store(tmp_path)

    first = lint_result(store, tmp_path)
    first["dangling"] = 999
    first["stale_repos"].append("injected")

    second = lint_result(store, tmp_path)
    assert second["dangling"] == 0
    assert "injected" not in second["stale_repos"]
    # ...and it is still JSON-serialisable, which is how the MCP tool returns it.
    json.dumps(second)


_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _checkout_store(tmp_path):
    """A store whose one repo is a real checkout, indexed at its current HEAD."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    _git(["init", "-q", "-b", "main"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "c"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(repo), head_commit=head))
    nodes = [Node(id="a", repo="r", kind="function", name="a", file="a.py", line_start=1),
             Node(id="b", repo="r", kind="function", name="b", file="b.py", line_start=1)]
    edges = [_edge("a", "b")]
    write_shard(tmp_path, GraphShard(repo="r", head_commit=head, nodes=nodes, edges=edges))
    store.upsert_nodes("r", nodes)
    store.upsert_edges("r", edges)
    return store, repo


def test_a_head_that_moves_is_reported_while_the_shards_are_unchanged(tmp_path):
    """`git pull` moves HEAD and rewrites no shard, so the shard fingerprint cannot see it.
    A long-lived `kb serve` or dashboard has to report the stale repo on its next call, not
    after a restart."""
    lint_mod._lint_cache.clear()
    store, repo = _checkout_store(tmp_path)
    assert lint_result(store, tmp_path)["stale"] == 0

    (repo / "a.py").write_text("def a():\n    return 2\n")
    _git(["commit", "-q", "-am", "c2"], repo)

    res = lint_result(store, tmp_path)
    assert res["stale"] == 1 and res["stale_repos"] == ["r"], (
        "a moved HEAD was answered from cache: staleness is being read from the shard "
        "fingerprint, which does not see git")


def test_a_checkout_that_disappears_is_reported_while_the_shards_are_unchanged(tmp_path):
    lint_mod._lint_cache.clear()
    store, repo = _checkout_store(tmp_path)
    first = lint_result(store, tmp_path)
    assert first["unreadable"] == 0 and first["checked"] == 1

    shutil.rmtree(repo)

    res = lint_result(store, tmp_path)
    assert res["unreadable"] == 1 and res["unreadable_repos"][0]["reason"] == "missing"
    assert res["checked"] == 1   # the shard part still comes from the cache


def test_the_cache_holds_the_count_and_a_capped_sample_of_dangling_edges(tmp_path):
    """A store with many dangling edges must not put the whole list in the cache, or every
    hit deep-copies it. The count stays whole and the sample stays at 20."""
    lint_mod._lint_cache.clear()
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "checkout")))
    nodes = [Node(id="a", repo="r", kind="function", name="a", file="a.py", line_start=1)]
    edges = [_edge("a", f"ghost{i}") for i in range(25)]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h", nodes=nodes, edges=edges))
    store.upsert_nodes("r", nodes)
    store.upsert_edges("r", edges)

    first = lint_result(store, tmp_path)
    second = lint_result(store, tmp_path)

    assert first["dangling"] == second["dangling"] == 25
    assert len(second["dangling_sample"]) == 20
    [(_, held)] = lint_mod._lint_cache.values()
    assert max(len(v) for v in held.values() if isinstance(v, list)) <= 20
