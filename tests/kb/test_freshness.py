"""Staleness here is silent by construction, so the check for it gets real tests.

`index` skips a repo whose head has not moved, which is the right default and also means
a store can be arbitrarily far behind while every command it serves looks healthy. This
project has shipped that twice: a stale index invisible after an upgrade, and derived
artefacts reporting themselves fresh across a parser-version change. So the interesting
assertions below are not "does it notice a moved commit" but the judgement calls: what
counts as stale, what a time budget does to honesty, and what an absent clone means.
"""

import json
import subprocess
from types import SimpleNamespace

from contextlake.kb import freshness
from contextlake.kb.model import Repo
from contextlake.kb.store.sqlite_store import SqliteStore


def _repo(path, *, commit="one"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", commit]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True, env=env,
                       capture_output=True)
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _store(tmp_path):
    return SqliteStore(tmp_path / "index.sqlite")


def _args(tmp_path, **over):
    """Point the command at the fixture store the way the CLI does -- through a config
    file. A bare `store_dir` attribute on the namespace is silently ignored (it is only
    an `init` flag), so a test that sets one runs against whatever store the ambient
    config resolves and asserts nothing."""
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path.as_posix()}"\n', encoding="utf-8")
    base = {"config": str(cfg), "hook": False, "json": False, "refresh": False,
            "budget": None, "workspace": None}
    return SimpleNamespace(**{**base, **over})


def test_a_repo_whose_head_moved_is_reported_and_named(tmp_path):
    head = _repo(tmp_path / "r")
    store = _store(tmp_path)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r"), head_commit=head))
    assert not freshness.check(store, tmp_path).is_stale

    (tmp_path / "r" / "b.py").write_text("x = 1\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-C", str(tmp_path / "r"), "add", "-A"], check=True, env=env,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path / "r"), "commit", "-qm", "two"],
                   check=True, env=env, capture_output=True)

    f = freshness.check(store, tmp_path)
    assert f.moved == ["r"] and f.is_stale
    assert "1 repo(s) moved" in f.summary()


def test_an_absent_clone_is_reported_but_is_not_staleness(tmp_path):
    """The judgement call. A repo whose clone is gone cannot be fixed by re-indexing,
    so counting it as stale would make every session start propose work that changes
    nothing -- and a proposal that never helps is one users learn to ignore."""
    store = _store(tmp_path)
    store.upsert_repo(Repo(id="gone", path=str(tmp_path / "not-here"), head_commit="abc"))
    f = freshness.check(store, tmp_path)
    assert f.unreadable == ["gone"]
    assert not f.is_stale
    assert "unreadable" in f.summary()


def test_a_directory_that_is_not_a_repo_is_unreadable_not_current(tmp_path):
    """`git rev-parse` failing must not read as "head has not moved"."""
    (tmp_path / "plain").mkdir()
    store = _store(tmp_path)
    store.upsert_repo(Repo(id="plain", path=str(tmp_path / "plain"), head_commit="abc"))
    assert freshness.check(store, tmp_path).unreadable == ["plain"]


def test_an_older_parser_is_stale_even_though_the_commit_has_not_moved(tmp_path):
    """The failure mode that shipped: the head matches, so `index` skips, so the graph
    stays whatever an older parser produced and nothing says so."""
    head = _repo(tmp_path / "r")
    store = _store(tmp_path)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r"), head_commit=head))
    store.mark_indexed("r", head, "2026-08-12T00:00:00Z", parser_version="0")
    f = freshness.check(store, tmp_path)
    assert f.stale_parser == ["r"] and f.moved == [] and f.is_stale


def test_the_time_budget_reports_what_it_did_not_check(tmp_path):
    """A cap nobody is told about reads as a clean bill of health for work that never
    happened, which is this project's signature bug. Budget 0 checks one repo (the
    deadline is tested before each, so the first always runs) and says so about the
    rest."""
    head = _repo(tmp_path / "r1")
    store = _store(tmp_path)
    for i in range(4):
        store.upsert_repo(Repo(id=f"r{i}", path=str(tmp_path / "r1"), head_commit=head))
    f = freshness.check(store, tmp_path, budget=0.0)
    assert f.checked + f.unchecked == 4
    assert f.unchecked > 0
    assert "not checked (time budget)" in f.summary()


def test_missing_vectors_are_a_fact_not_a_problem(tmp_path):
    """Plenty of stores never enable embeddings; flagging that as staleness would
    nag every session forever."""
    head = _repo(tmp_path / "r")
    store = _store(tmp_path)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r"), head_commit=head))
    f = freshness.check(store, tmp_path)
    assert f.vectors_missing and not f.vectors_stale and not f.is_stale


def test_the_hook_form_prints_the_json_shape_claude_code_reads(tmp_path, capsys):
    """The contract that makes this useful: a SessionStart hook's stdout reaches the
    model only in this shape. Asserted structurally, because a stray log line on stdout
    would break the parse and nothing else would notice."""
    from contextlake.kb.cmds.refresh import cmd_refresh

    head = _repo(tmp_path / "r")
    store = _store(tmp_path)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r"), head_commit=head))
    store.close()

    assert cmd_refresh(_args(tmp_path, hook=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    # "1 repositories" pins that the fixture store was opened. Without this the test
    # passes just as happily against whatever store the ambient config resolves, which
    # is how a test ends up asserting nothing about the code under test.
    assert "1 repositories" in ctx or "all 1 repositories" in ctx, ctx


def test_the_hook_can_be_switched_off_by_environment(tmp_path, capsys, monkeypatch):
    """A hook somebody cannot turn off in one command is a hook they delete. Silent
    when disabled: an announcement on every session start is noise."""
    from contextlake.kb.cmds.refresh import DISABLE_ENV, cmd_refresh

    monkeypatch.setenv(DISABLE_ENV, "1")
    assert cmd_refresh(_args(tmp_path, hook=True)) == 0
    assert capsys.readouterr().out == ""
