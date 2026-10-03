"""`kb forget` must find module wiki pages by the name the wiki WRITER gave them.

The writer names a module page `<_safe_name(repo)>__<_safe_name(prefix)>.md`, and
`_safe_name` folds every character that is not a word character, `.` or `-` to `_`. The
reader globbed on `repo_id.replace("/", "__")` alone, so an id holding `@` (a remote-less
repo is `name@<commit>`) or `:` (a host with a port) never matched its own module pages.
The rows and shards were forgotten and the module pages stayed on disk.
"""

from __future__ import annotations

import re
import types

import pytest

from contextlake.kb.cmds.forget import _wiki_pages, cmd_forget
from contextlake.kb.cmds.wiki import _module_page_file, cmd_wiki
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize import repo_slug

# Ids as the indexer produces them, not invented ones: a remote-less repo is `name@commit`,
# a self-hosted forge on a port carries the port, and the rest cover other characters the
# writer folds.
HARD_IDS = ["name@abc123def", "host:8080/x", "git.example.com:2222/grp/app",
            "team/app with space", "team/ünï", "plain/repo"]


@pytest.mark.parametrize("repo_id", HARD_IDS)
def test_wiki_pages_finds_every_page_the_writer_names(tmp_path, repo_id):
    """The pairing test: pages are named by the writer's own functions, never by a literal
    copy of its rule, so a change to the writer that the reader misses fails here."""
    wiki = tmp_path / "wiki"
    own = []
    for prefix in ("src", "src/foo", "a b"):
        page = _module_page_file(wiki, repo_id, prefix)
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("module", encoding="utf-8")
        own.append(page)
    whole = wiki / (repo_slug(repo_id) + ".md")
    whole.write_text("whole", encoding="utf-8")
    own.append(whole)
    # A neighbour that must survive: a different repo, written by the same writer.
    other = _module_page_file(wiki, "zzz/other", "src")
    other.write_text("other", encoding="utf-8")

    got = _wiki_pages(wiki, repo_id)

    assert sorted(got) == sorted(own)
    assert other not in got


def test_an_id_with_a_glob_character_does_not_match_other_repos_pages(tmp_path):
    """Kept from the glob.escape fix. The writer folds `*` and `[` to `_`, so the pattern
    never holds them today, but the reader must still treat the id as data."""
    wiki = tmp_path / "wiki"
    for repo_id in ("host/*", "host/other", "host/[ab]"):
        page = _module_page_file(wiki, repo_id, "mod")
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(repo_id, encoding="utf-8")

    got = {p.read_text(encoding="utf-8") for p in _wiki_pages(wiki, "host/other")}

    assert got == {"host/other"}


# --- end to end: the real writer, then the real forget ---------------------------------

_CFG = '[kb]\nstore_dir = "{store}"\n\n[embeddings]\nenabled = false\n'


def _index(store, store_dir, tmp_path, repo_id, pages):
    """One small repo whose `wiki.toml` names ``pages``, so the structural stage writes its
    module pages through the real writer."""
    repo_dir = tmp_path / ("w_" + re.sub(r"\W", "_", repo_id))
    (repo_dir / ".contextlake").mkdir(parents=True)
    listed = ", ".join(f'"{p}"' for p in pages)
    (repo_dir / ".contextlake" / "wiki.toml").write_text(f"pages = [{listed}]\n",
                                                         encoding="utf-8")
    store.upsert_repo(Repo(id=repo_id, path=str(repo_dir)))
    nodes = [Node(id=f"{repo_id}:{m}:{i}", repo=repo_id, kind="function", name=f"fn{i}",
                  file=f"{m}/f{i}.py") for m in pages for i in range(12)]
    store.upsert_nodes(repo_id, nodes)
    write_shard(store_dir, GraphShard(repo=repo_id, head_commit="abc", nodes=nodes,
                                      edges=[]))


@pytest.mark.parametrize("repo_id", ["name@abc123def", "host:8080/x"])
def test_forget_removes_the_module_pages_the_wiki_command_wrote(tmp_path, monkeypatch,
                                                                repo_id):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    (tmp_path / "kb.toml").write_text(_CFG.format(store=store_dir.as_posix()),
                                      encoding="utf-8")
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    _index(store, store_dir, tmp_path, repo_id, ["mod1", "mod2"])
    _index(store, store_dir, tmp_path, "keep/me", ["mod1"])
    store.close()
    assert cmd_wiki(types.SimpleNamespace(config=str(tmp_path / "kb.toml"))) == 0

    modules = store_dir / "wiki" / "_modules"
    mine = {_module_page_file(store_dir / "wiki", repo_id, p) for p in ("mod1", "mod2")}
    kept = _module_page_file(store_dir / "wiki", "keep/me", "mod1")
    assert all(p.exists() for p in mine), sorted(x.name for x in modules.iterdir())
    assert kept.exists()

    rc = cmd_forget(types.SimpleNamespace(
        config=str(tmp_path / "kb.toml"), repo=repo_id, dry_run=False, verbose=False,
        quiet=True, json=False))

    assert rc == 0
    left = sorted(p.name for p in modules.iterdir())
    assert not any(p.exists() for p in mine), f"orphan module pages remain: {left}"
    assert not (store_dir / "wiki" / (repo_slug(repo_id) + ".md")).exists()
    assert kept.exists(), "another repo's module page was removed"
