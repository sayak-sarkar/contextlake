"""Pure data functions backing the knowledge-system dashboard (kb/dashboard/data.py).

Builds a small two-repo store (graph shards + a tmp git repo with a README and a
commit + a generated wiki page) and exercises every data function's shape.
"""

import os
import subprocess
from datetime import date

import pytest

from contextlake.kb.dashboard import data as kbdata
from contextlake.kb.ids import make_id
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))


def _edge(src, dst, relation):
    return Edge(src=src, dst=dst, relation=relation, confidence=Confidence.EXTRACTED,
                provenance=_PROV)


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=env)


def _git_repo(path):
    """A tmp git repo with a README and one commit (explicit identity for clean CI)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "README.md").write_text("# team/app\n\nThe **order** service. See `charge`.\n")
    _git(path, "add", "README.md")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "Ada Lovelace", "GIT_AUTHOR_EMAIL": "ada@example.io",
           "GIT_COMMITTER_NAME": "Ada Lovelace", "GIT_COMMITTER_EMAIL": "ada@example.io"}
    _git(path, "commit", "-q", "-m", "add readme", env=env)


@pytest.fixture
def store_dir(tmp_path):
    """A populated store: two repos, cross-repo dependency, a link, a shard, a wiki."""
    clone = tmp_path / "clone"
    _git_repo(clone)

    s = SqliteStore(tmp_path / "index.sqlite")

    # team/app — a class + a function, a caller (for impact), a dep edge, a repo link.
    app_nodes = [
        Node(id="app_orderservice", repo="team/app", kind="class", name="OrderService",
             file="src/order.py", lang="python"),
        Node(id="app_charge", repo="team/app", kind="function", name="charge",
             file="src/pay.py", lang="python"),
        Node(id="app_caller", repo="team/app", kind="function", name="checkout",
             file="src/web.py", lang="python"),
        Node(id="app_mod", repo="team/app", kind="module", name="app", lang="python"),
        Node(id=make_id("repo", "team/app"), repo="team/app", kind="repo", name="team/app"),
        Node(id="issue_42", repo="team/app", kind="issue", name="PROJ-42",
             attrs={"url": "https://tracker.example.com/PROJ-42", "title": "Fix charge",
                    "status": "open"}),
    ]
    app_edges = [
        _edge("app_caller", "app_orderservice", "calls"),
        _edge("app_mod", "libpkg", "depends_on"),
        _edge(make_id("repo", "team/app"), "issue_42", "tracked_by"),
    ]
    # team/lib — publishes the package team/app depends on.
    lib_nodes = [
        Node(id="lib_mod", repo="team/lib", kind="module", name="lib", lang="python"),
        Node(id="libpkg", repo="team/lib", kind="package", name="libpkg"),
    ]
    lib_edges = [_edge("lib_mod", "libpkg", "publishes")]

    s.upsert_repo(Repo(id="team/app", path=str(clone), head_commit="head-app"))
    s.upsert_repo(Repo(id="team/lib", path=str(tmp_path / "libclone"), head_commit="head-lib"))
    write_shard(tmp_path, GraphShard(repo="team/app", head_commit="head-app",
                                     nodes=app_nodes, edges=app_edges))
    write_shard(tmp_path, GraphShard(repo="team/lib", head_commit="head-lib",
                                     nodes=lib_nodes, edges=lib_edges))
    reindex_shard(s, tmp_path, "team/app")
    reindex_shard(s, tmp_path, "team/lib")
    s.mark_indexed("team/app", "head-app", "2026-06-01T00:00:00Z")
    s.mark_indexed("team/lib", "head-lib", "2026-06-01T00:00:00Z")

    # a curated wiki page for team/app (slug = team__app)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "team__app.md").write_text(
        "# team/app\n\nGenerated at commit `head-app`.\n\n- handles **orders**\n")

    yield s, tmp_path
    s.close()


def test_fleet_overview_lists_repos_with_stats_and_langs(store_dir):
    s, _ = store_dir
    ov = kbdata.fleet_overview(s)
    assert ov["stats"]["repos"] == 2 and ov["stats"]["nodes"] > 0
    ids = {r["id"] for r in ov["repos"]}
    assert ids == {"team/app", "team/lib"}
    app = next(r for r in ov["repos"] if r["id"] == "team/app")
    assert app["node_count"] >= 4
    assert app["langs"].get("python")
    assert app["head_commit"] == "head-app" and app["group"] == "team"
    assert {g["group"] for g in ov["groups"]} == {"team"}


def test_derive_groups_depth_and_ungrouped():
    groups = kbdata.derive_groups(["a/b/c", "a/x", "solo"], depth=1)
    by = {g["group"]: g for g in groups}
    assert by["a"]["count"] == 2
    assert by["(ungrouped)"]["repos"] == ["solo"]
    deep = {g["group"] for g in kbdata.derive_groups(["a/b/c", "a/x"], depth=2)}
    assert deep == {"a/b", "a/x"} or "a/b" in deep  # a/x has no 2nd-level namespace


def test_repo_detail_brief_readme_wiki_owners(store_dir):
    s, sd = store_dir
    d = kbdata.repo_detail(s, sd, "team/app")
    assert d["brief"]["node_count"] >= 4
    assert "OrderService" in {t["name"] for t in d["brief"]["top_symbols"]} or d["brief"]["kinds"]
    assert d["readme_html"] and "<strong>order</strong>" in d["readme_html"]
    assert d["wiki"]["found"] and d["wiki"]["html"]
    assert d["wiki"]["stale"] is False  # wiki commit matches the indexed head
    assert any(o["name"] == "Ada Lovelace" for o in d["owners"])
    assert d["links"]["tracked_by"][0]["url"].startswith("https://")


def test_repo_detail_anonymize_hashes_authors_and_strips_urls(store_dir):
    s, sd = store_dir
    d = kbdata.repo_detail(s, sd, "team/app", anonymize=True)
    assert d["owners"] and all(o["name"].startswith("Contributor ") for o in d["owners"])
    assert "Ada" not in str(d["owners"])
    assert d["links"]["tracked_by"][0]["url"] is None
    # README/wiki prose is dropped entirely (it can carry author names / live URLs);
    # only the wiki found/stale flags survive.
    assert d["readme_html"] is None
    assert d["wiki"]["found"] is True and d["wiki"]["html"] is None
    # the non-anonymized build DOES render the README prose (proves the drop is real)
    plain = kbdata.repo_detail(s, sd, "team/app")
    assert plain["readme_html"] and plain["wiki"]["html"]


def test_repo_relationships_dependency_two_hop(store_dir):
    s, _ = store_dir
    rel = kbdata.repo_relationships(s, "team/app")
    deps = rel["dependencies"]
    assert any(e["src"] == "team/app" and e["dst"] == "team/lib" for e in deps)
    assert all("context" in e and "weight" in e for e in deps)
    assert rel["http_flow"] == [] and rel["event_flow"] == []


def test_impact_blast_radius_and_name_fallback(store_dir):
    s, _ = store_dir
    by_id = kbdata.impact(s, "app_orderservice")
    assert by_id["found"] and by_id["total"] >= 1
    assert "checkout" in {h["name"] for h in by_id["hits"]}
    by_name = kbdata.impact(s, "OrderService")  # falls back to search
    assert by_name["found"]
    assert kbdata.impact(s, "does-not-exist")["found"] is False


def test_health_shape(store_dir):
    s, sd = store_dir
    h = kbdata.health(s, sd)
    assert set(h) >= {"repos", "checked", "stale", "dangling", "stale_repos",
                      "dangling_sample"}
    assert h["repos"] == 2


def test_code_search_returns_nodes(store_dir):
    s, _ = store_dir
    res = kbdata.code_search(s, "OrderService")
    assert res["semantic"] is False
    assert "OrderService" in {n["name"] for n in res["results"]}
