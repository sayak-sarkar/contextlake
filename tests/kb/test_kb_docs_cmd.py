"""`contextlake kb docs`: what it writes, what it reports, and what it exits with.

Every store the renderer was developed against held exactly ONE repository, which is the
configuration that cannot show a partial run. These tests are the multi-repo ones, because
the interesting states only exist there:

- one repo written and another unreadable, which must not exit 0
- one repo written and another genuinely empty, which must
- nothing written at all

The distinction between an EMPTY shard and a MISSING one carries the exit code, so it is
asserted directly. Reporting both as "indexed to 0 symbols" states a cause that is false half
the time, and the half it hides is the one that needs a person.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import date

from contextlake.kb.cmds.docs import cmd_docs
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_CFG = '[kb]\nstore_dir = "{store}"\n'


def _shard_for(store_dir, repo_id, *, nodes=True):
    """A shard with one documented symbol and one real call site, or an empty one."""
    if not nodes:
        write_shard(store_dir, GraphShard(repo=repo_id, head_commit="h0",
                                          nodes=[], edges=[]))
        return
    write_shard(store_dir, GraphShard(
        repo=repo_id, head_commit="h1",
        nodes=[
            Node(id=f"{repo_id}:t", repo=repo_id, kind="function", name="encode",
                 file="codec.py", line_start=10, line_end=20),
            Node(id=f"{repo_id}:c", repo=repo_id, kind="function", name="drive",
                 file="driver.py", line_start=1, line_end=5),
        ],
        edges=[Edge(src=f"{repo_id}:c", dst=f"{repo_id}:t", relation="calls",
                    confidence=Confidence.EXTRACTED,
                    provenance=Provenance(source_file="driver.py", source_line=3,
                                          verified_at=date(2026, 8, 17)))],
    ))


def _store(tmp_path, repos):
    """``repos`` maps repo id -> "full" | "empty" | "missing" (no shard written)."""
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(_CFG.format(store=store_dir.as_posix()))
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    for rid, shape in repos.items():
        store.upsert_repo(Repo(id=rid, path=str(tmp_path / rid.replace("/", "_"))))
        if shape == "full":
            _shard_for(store_dir, rid)
        elif shape == "empty":
            _shard_for(store_dir, rid, nodes=False)
    store.close()
    return store_dir


def _run(tmp_path, **kw):
    return cmd_docs(Namespace(config=str(tmp_path / "kb.toml"), **kw))


def test_writes_one_file_per_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _store(tmp_path, {"team/a": "full", "team/b": "full"})

    assert _run(tmp_path) == 0
    written = sorted(p.name for p in (store_dir / "docs" / "api").glob("*.md"))
    assert len(written) == 2
    # The content is the reference, not an empty placeholder.
    body = (store_dir / "docs" / "api" / written[0]).read_text()
    assert "API reference" in body and "call site(s)" in body


def test_an_unreadable_shard_exits_non_zero_even_when_another_repo_was_written(
        tmp_path, monkeypatch, gls_logs):
    """A partial run is not a clean run.

    This is the case a single-repo store cannot produce, and it is the one that matters: a
    script that reads the exit code would be told nine of ten repositories is success.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _store(tmp_path, {"team/a": "full", "team/gone": "missing"})

    assert _run(tmp_path) == 1
    # The good repo is still written: one broken shard must not discard the work that
    # succeeded. Both documents, since the command produces a reference AND design notes,
    # and a partial run has to be countable per repository rather than per file.
    assert list((store_dir / "docs" / "api").glob("*.md"))
    assert list((store_dir / "docs" / "design").glob("*.md"))
    assert "1 of each written" in gls_logs.text
    assert "1 unreadable" in gls_logs.text


def test_an_empty_shard_is_reported_but_is_not_a_failure(tmp_path, monkeypatch, gls_logs):
    """A repository that indexed to no symbols has no interface, which is not an error."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store(tmp_path, {"team/a": "full", "team/b": "empty"})

    assert _run(tmp_path) == 0
    assert "skipped (nothing indexed)" in gls_logs.text
    assert "unreadable" not in gls_logs.text


def test_an_empty_shard_and_a_missing_one_are_reported_differently(
        tmp_path, monkeypatch, gls_logs):
    """The two lines must not be interchangeable: only one of them names a broken store."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store(tmp_path, {"team/empty": "empty", "team/gone": "missing"})

    assert _run(tmp_path) == 1
    assert "indexed to 0 symbols" in gls_logs.text
    assert "could not be read" in gls_logs.text


def test_nothing_written_at_all_exits_non_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _store(tmp_path, {"team/b": "empty"})

    assert _run(tmp_path) == 1


def test_no_indexed_repo_is_not_a_failure(tmp_path, monkeypatch, gls_logs):
    """An empty store means "run index first", which is guidance and not a fault."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store(tmp_path, {})

    assert _run(tmp_path) == 0
    assert "No indexed repos" in gls_logs.text


def test_max_symbols_bounds_the_document_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _store(tmp_path, {"team/a": "full"})

    assert _run(tmp_path, max_symbols=1) == 0
    body = next((store_dir / "docs" / "api").glob("*.md")).read_text()
    assert "1 of 2 callable symbols" in body
    assert "are not listed" in body
