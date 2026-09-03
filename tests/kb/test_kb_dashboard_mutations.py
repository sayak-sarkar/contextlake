"""Dashboard mutating actions (kb/dashboard/mutations.py): sync/add a repo, the
HTTP-transport MCP server lifecycle. Against real throwaway git repos -- these
functions shell out to `git`, and the whole point of validate_clone_url is to
prove specific malicious inputs get rejected before they reach subprocess argv.
"""

import json
import subprocess
import time

import pytest

from contextlake.kb.dashboard import mutations as mut
from contextlake.kb.model import Repo
from contextlake.kb.store.sqlite_store import SqliteStore


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(path, *, with_remote_origin=None):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "a.py").write_text("def a():\n    pass\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    if with_remote_origin:
        _git(path, "remote", "add", "origin", with_remote_origin)
    return path


# --- validate_clone_url -------------------------------------------------

def test_validate_clone_url_accepts_https():
    assert mut.validate_clone_url("https://example.com/org/repo.git") is None


def test_validate_clone_url_accepts_ssh():
    assert mut.validate_clone_url("ssh://git@example.com/org/repo.git") is None


def test_validate_clone_url_accepts_scp_like():
    assert mut.validate_clone_url("git@example.com:org/repo.git") is None


def test_validate_clone_url_rejects_flag_injection():
    reason = mut.validate_clone_url("--upload-pack=touch /tmp/pwned")
    assert reason is not None


def test_validate_clone_url_rejects_ext_transport():
    reason = mut.validate_clone_url("ext::sh -c touch /tmp/pwned")
    assert reason is not None


def test_validate_clone_url_rejects_file_scheme():
    assert mut.validate_clone_url("file:///etc/passwd") is not None


def test_validate_clone_url_rejects_empty():
    assert mut.validate_clone_url("") is not None


# --- sync_repo -----------------------------------------------------------

def test_sync_repo_unknown_repo(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    result = mut.sync_repo(store, tmp_path, "no/such/repo")
    assert result == {"ok": False, "error": "unknown repo: no/such/repo"}
    store.close()


def test_sync_repo_pulls_and_reindexes(tmp_path):
    origin = _init_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)

    store = SqliteStore(tmp_path / "index.sqlite")
    head_before = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    store.upsert_repo(Repo(id="acme/app", path=str(clone), head_commit=head_before))
    store.mark_indexed("acme/app", head_before, "2026-01-01T00:00:00Z")

    # No new commits: nothing to reindex.
    result = mut.sync_repo(store, tmp_path, "acme/app")
    assert result == {"ok": True, "repo": "acme/app", "changed": False,
                      "message": "already up to date"}

    # New commit on origin -> pull should bring it in and trigger a reindex.
    (origin / "b.py").write_text("def b():\n    pass\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "add b")

    result = mut.sync_repo(store, tmp_path, "acme/app")
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["nodes"] >= 2
    store.close()


# --- add_repo --------------------------------------------------------------

def test_add_repo_rejects_bad_url(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    result = mut.add_repo(store, tmp_path, tmp_path / "ws", "--upload-pack=x")
    assert result["ok"] is False
    store.close()


def test_add_repo_clones_and_indexes(tmp_path, monkeypatch):
    # validate_clone_url intentionally excludes bare local paths (disclosure risk
    # over the real HTTP route -- see the tests above); bypass just the gate here
    # to exercise the clone+index mechanics against a fast, network-free origin.
    # resolve_repo_id is repo_identity.py's own concern (tested there) -- a locally
    # cloned origin has no real remote host/path to normalize, so pin a canonical
    # id here rather than asserting on that unrelated behaviour.
    monkeypatch.setattr(mut, "validate_clone_url", lambda url: None)
    monkeypatch.setattr("contextlake.kb.repo_identity.resolve_repo_id", lambda path: "acme/origin")
    origin = _init_repo(tmp_path / "origin")
    store = SqliteStore(tmp_path / "index.sqlite")
    workspace = tmp_path / "ws"

    result = mut.add_repo(store, tmp_path, workspace, str(origin))
    assert result["ok"] is True
    assert result["repo"] == "acme/origin"
    assert (workspace / "origin").is_dir()
    assert result["nodes"] >= 1
    assert store.get_repo("acme/origin") is not None
    store.close()


def test_add_repo_refuses_existing_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(mut, "validate_clone_url", lambda url: None)
    origin = _init_repo(tmp_path / "origin")
    store = SqliteStore(tmp_path / "index.sqlite")
    workspace = tmp_path / "ws"
    (workspace / "origin").mkdir(parents=True)

    result = mut.add_repo(store, tmp_path, workspace, str(origin))
    assert result["ok"] is False
    assert "already exists" in result["error"]
    store.close()


# --- MCP HTTP-transport server lifecycle ------------------------------------

def test_mcp_status_when_never_started(tmp_path):
    assert mut.mcp_status(tmp_path) == {"running": False}


@pytest.mark.slow
def test_mcp_start_stop_restart_lifecycle(tmp_path):
    store_dir = tmp_path
    SqliteStore(store_dir / "index.sqlite").close()

    started = mut.mcp_start(store_dir, port=0)
    # port=0 asks the OS for a free port at bind time, but our subprocess wrapper
    # doesn't resolve the actual bound port -- just prove the lifecycle plumbing
    # (pidfile, alive-check, stop) works without depending on the real server
    # binding successfully in this sandbox (no network guarantee in CI).
    assert "pid" in started or "error" in started
    if started.get("ok"):
        status = mut.mcp_status(store_dir)
        assert status["running"] is True
        stopped = mut.mcp_stop(store_dir)
        assert stopped == {"ok": True, "running": False}
        assert mut.mcp_status(store_dir) == {"running": False}


def test_mcp_start_refuses_when_already_running(tmp_path, monkeypatch):
    pf = mut._mcp_pidfile(tmp_path)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text('{"pid": ' + str(_alive_pid()) + ', "host": "127.0.0.1", "port": 8766}')
    result = mut.mcp_start(tmp_path)
    assert result["ok"] is False
    assert result["error"] == "already running"


def test_mcp_status_ignores_stale_pidfile(tmp_path):
    pf = mut._mcp_pidfile(tmp_path)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text('{"pid": 999999999, "host": "127.0.0.1", "port": 8766}')
    assert mut.mcp_status(tmp_path) == {"running": False}


def test_mcp_start_pins_a_token_the_dashboard_can_show(tmp_path, monkeypatch):
    """The child's stderr is DEVNULL'd, so if it minted its own bearer token the
    dashboard would start a server nobody could authenticate to. The token must
    be minted here, passed through the child's env, and survive in the pidfile so
    the card still has it after a page reload."""
    from contextlake.kb.server import TOKEN_ENV

    seen = {}

    class _Proc:
        pid = _alive_pid()

        def poll(self):
            return None  # still running

    def _fake_popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return _Proc()

    monkeypatch.setattr(mut.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(mut.time, "sleep", lambda _s: None)

    started = mut.mcp_start(tmp_path, port=8766)

    token = seen["env"][TOKEN_ENV]
    assert token and started["token"] == token
    assert "--transport" in seen["cmd"] and "http" in seen["cmd"]
    # Survives a reload: the status endpoint re-reads it from the pidfile.
    assert mut.mcp_status(tmp_path)["token"] == token
    # It is a credential on disk now, so the pidfile must not be world-readable.
    assert (mut._mcp_pidfile(tmp_path).stat().st_mode & 0o077) == 0


def _alive_pid():
    """A pid guaranteed alive for the duration of the test: our own process."""
    import os
    return os.getpid()


# --- Live wiki generation ----------------------------------------------------

def _shard_with_wiki(store_dir, repo_id, *, head="abc123", wiki_head=None):
    """An indexed repo, optionally with an existing wiki page stamped at
    ``wiki_head`` (None = no existing page -- always "would regenerate")."""
    from datetime import date

    from contextlake.kb.model import Confidence, Edge, Node, Provenance
    from contextlake.kb.store.shards import GraphShard, write_shard

    nodes = [Node(id="a", repo=repo_id, kind="function", name="a", file="a.py"),
            Node(id="b", repo=repo_id, kind="function", name="b", file="a.py")]
    edges = [Edge(src="a", dst="b", relation="calls", confidence=Confidence.EXTRACTED,
                 provenance=Provenance(source_file="a.py", source_line=1,
                                       verified_at=date(2026, 6, 21)))]
    write_shard(store_dir, GraphShard(repo=repo_id, head_commit=head, nodes=nodes, edges=edges))
    if wiki_head is not None:
        wiki_dir = store_dir / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        wiki_file = wiki_dir / (repo_id.replace("/", "__") + ".md")
        wiki_file.write_text(f"# {repo_id}\n\nBody.\n\n---\n"
                             f"*Generated ... at commit `{wiki_head}` on 2026-07-30.*")


def test_wiki_generate_status_when_never_started(tmp_path):
    assert mut.wiki_generate_status(tmp_path) == {"running": False, "log_tail": ""}


def test_wiki_generate_status_ignores_stale_pidfile(tmp_path):
    pf = mut._wiki_pidfile(tmp_path)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text('{"pid": 999999999}')
    status = mut.wiki_generate_status(tmp_path)
    assert status["running"] is False
    assert not pf.exists()   # cleared once reported


def test_wiki_generate_status_log_tail_survives_pidfile_being_cleared(tmp_path):
    """The pidfile is a one-shot liveness signal (cleared once a finished run
    is reported); the log is the durable record. A poller that misses that one
    tick (a second tab, a page reload) must still see the log -- including a
    failure -- not go blank just because the pidfile is already gone."""
    log = mut._wiki_logfile(tmp_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("some real progress or failure output")
    # no pidfile at all -- as if a prior poll already reported finished and cleared it
    status = mut.wiki_generate_status(tmp_path)
    assert status == {"running": False, "log_tail": "some real progress or failure output"}


def test_wiki_generate_start_refuses_when_already_running(tmp_path):
    pf = mut._wiki_pidfile(tmp_path)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text('{"pid": ' + str(_alive_pid()) + '}')
    result = mut.wiki_generate_start(tmp_path)
    assert result["ok"] is False
    assert "already in progress" in result["error"]


def test_wiki_generate_estimate_unchanged_repo_is_not_counted(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(tmp_path)))
    _shard_with_wiki(tmp_path, "r", head="abc123", wiki_head="abc123")
    result = mut.wiki_generate_estimate(store, tmp_path)
    assert result == {"total": 1, "would_regenerate": 0, "unchanged": 1}
    store.close()


def test_wiki_generate_estimate_stale_repo_is_counted(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(tmp_path)))
    _shard_with_wiki(tmp_path, "r", head="new_sha", wiki_head="old_sha")
    result = mut.wiki_generate_estimate(store, tmp_path)
    assert result == {"total": 1, "would_regenerate": 1, "unchanged": 0}
    store.close()


def test_wiki_generate_estimate_missing_wiki_page_is_counted(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(tmp_path)))
    _shard_with_wiki(tmp_path, "r", head="abc123", wiki_head=None)
    result = mut.wiki_generate_estimate(store, tmp_path)
    assert result == {"total": 1, "would_regenerate": 1, "unchanged": 0}
    store.close()


def test_wiki_generate_estimate_force_counts_everything(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(tmp_path)))
    _shard_with_wiki(tmp_path, "r", head="abc123", wiki_head="abc123")
    result = mut.wiki_generate_estimate(store, tmp_path, force=True)
    assert result == {"total": 1, "would_regenerate": 1, "unchanged": 0}
    store.close()


def test_wiki_generate_estimate_scopes_to_one_repo(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r1", path=str(tmp_path)))
    store.upsert_repo(Repo(id="r2", path=str(tmp_path)))
    _shard_with_wiki(tmp_path, "r1", head="abc123", wiki_head="abc123")
    _shard_with_wiki(tmp_path, "r2", head="new_sha", wiki_head="old_sha")
    result = mut.wiki_generate_estimate(store, tmp_path, repo_id="r1")
    assert result == {"total": 1, "would_regenerate": 0, "unchanged": 1}
    store.close()


def test_wiki_generate_start_stop_lifecycle_on_empty_store(tmp_path):
    """No indexed repos -> the real CLI exits almost instantly (no LLM call
    attempted) -- proves the Popen/pidfile/log plumbing works end to end
    without needing a configured LLM provider in the test sandbox.

    Passes an explicit isolated --config so the subprocess can never fall
    back to a real (possibly production) store via the normal discovery
    chain -- see wiki_generate_start's docstring.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    SqliteStore(store_dir / "index.sqlite").close()
    config_path = tmp_path / "kb.toml"
    config_path.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')

    started = mut.wiki_generate_start(store_dir, config_path=str(config_path))
    assert started.get("ok") is True
    assert "pid" in started
    for _ in range(50):
        status = mut.wiki_generate_status(store_dir)
        if status.get("finished"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("wiki generation subprocess did not finish in time")
    assert status["running"] is False
    assert not mut._wiki_pidfile(store_dir).exists()


def test_wiki_generate_estimate_does_not_count_a_repo_the_run_will_refuse(tmp_path):
    """The estimate promises a lower bound: a run regenerates at least as many
    as it said, never fewer. A repo with nothing behind it is refused by the run
    itself, so counting it here would make the dashboard promise a page that is
    never going to be written."""
    from contextlake.kb.store.shards import GraphShard, write_shard

    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_repo(Repo(id="r", path=str(tmp_path)))
    write_shard(tmp_path, GraphShard(repo="r", head_commit="abc123", nodes=[], edges=[]))
    result = mut.wiki_generate_estimate(store, tmp_path)
    assert result["would_regenerate"] == 0
    store.close()


# --- Live document generation ------------------------------------------------
#
# `kb docs` is a different shape from `kb wiki` in three ways these tests pin:
# the refusal payload keeps the pid AND a start time (wiki_generate_status strips
# the pid and the wiki pidfile records no start time), and the result is read back
# from the files the run wrote, never from the child's exit code.


def _docs_store(tmp_path, repo_id="r"):
    """A store with one indexed repo, plus an isolated HOME and an explicit config.

    PRECONDITION: both isolations are load-bearing, not decoration. When the
    break-tests below delete a guard, ``docs_generate_start`` spawns a real
    ``contextlake kb docs``, and the guard being deleted is the guard against that
    spawn resolving a discovered store instead of this temp one.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    store = SqliteStore(store_dir / "index.sqlite")
    store.upsert_repo(Repo(id=repo_id, path=str(tmp_path)))
    _shard_with_wiki(store_dir, repo_id, head="abc123")
    store.close()
    config_path = tmp_path / "kb.toml"
    config_path.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    return store_dir, str(config_path)


def _await_finished(store_dir):
    for _ in range(50):
        status = mut.docs_generate_status(store_dir)
        if status.get("finished"):
            return status
        time.sleep(0.1)
    raise AssertionError("docs generation subprocess did not finish in time")


def test_docs_generate_status_when_never_started(tmp_path):
    """No run has ever started, so no ``documents_written`` key is invented with
    nothing behind it. An absent field reading as a pass is the failure this
    guards: `== 0 documents` and `no run happened` are different answers."""
    assert mut.docs_generate_status(tmp_path) == {"running": False, "log_tail": ""}


def test_docs_generate_start_refuses_a_second_run_and_names_the_pid(tmp_path, monkeypatch):
    """The refusal carries the running pid AND its start time.

    Copying ``wiki_generate_start``'s refusal verbatim cannot satisfy that:
    ``wiki_generate_status`` filters ``pid`` out of its payload, and the wiki
    pidfile records no start time at all. The docs payload diverges on both fields
    on purpose, and these two assertions are what pin the divergence.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path)
    started_at = 1756800000.5
    pf = mut._docs_pidfile(store_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(f'{{"pid": {_alive_pid()}, "started_at": {started_at!r}}}')

    result = mut.docs_generate_start(store_dir, config_path=config_path)

    assert result["ok"] is False
    assert "already in progress" in result["error"]
    assert result["pid"] == _alive_pid()
    assert result["started_at"] == started_at


def test_docs_generate_reports_two_documents_for_a_repo_scoped_run(tmp_path, monkeypatch):
    """A repo-scoped run writes the API reference and the design notes: 2, not 3.

    ``cmd_docs`` writes the fleet page only when the run covered every indexed repo,
    so the count is scope-dependent. Asserted alongside the files themselves, and
    the child's return code is never read.

    PRECONDITION: the count compares ``st_mtime`` against ``int(started_at)``, so a
    filesystem with 1-second mtime granularity still counts this run's output.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path)

    started = mut.docs_generate_start(store_dir, repo_id="r", config_path=config_path)
    assert started["ok"] is True
    status = _await_finished(store_dir)

    assert status["documents_written"] == 2
    api = store_dir / "docs" / "api" / "r.md"
    design = store_dir / "docs" / "design" / "r.md"
    assert api.is_file() and api.read_text(encoding="utf-8").strip()
    assert design.is_file() and design.read_text(encoding="utf-8").strip()
    assert not (store_dir / "docs" / "fleet" / "design.md").exists()


def test_docs_generate_counts_the_fleet_page_on_an_unscoped_run(tmp_path, monkeypatch):
    """An unscoped run over the same one-repo store writes 3, because it also writes
    the fleet page. Paired with the repo-scoped row above on purpose: with one
    shared expected number, a stubbed constant would satisfy both."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path)

    started = mut.docs_generate_start(store_dir, repo_id=None, config_path=config_path)
    assert started["ok"] is True
    status = _await_finished(store_dir)

    assert status["documents_written"] == 3
    assert (store_dir / "docs" / "fleet" / "design.md").is_file()


def test_the_count_ignores_documents_that_were_already_on_disk(tmp_path, monkeypatch):
    """The mtime cutoff has to FILTER, not just count what it finds.

    Both rows above start from a store with no ``docs/`` tree, so every file found
    is the run's own and a cutoff that filtered nothing would pass them. Here repo
    "a" already has both its documents, backdated an hour, and the run is scoped to
    repo "b": the answer is 2.

    Two guards hold that 2 now, and this row cannot tell them apart: the count is
    scoped to repo "b"'s two paths AND the cutoff filters. The row below
    (``test_the_cutoff_still_filters_on_an_unscoped_run``) is the one that pins the
    cutoff on its own, on a run with no scope to hide behind.
    """
    import os

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path, repo_id="a")
    store = SqliteStore(store_dir / "index.sqlite")
    store.upsert_repo(Repo(id="b", path=str(tmp_path)))
    store.close()
    _shard_with_wiki(store_dir, "b", head="def456")

    old = time.time() - 3600
    for kind in ("api", "design"):
        d = store_dir / "docs" / kind
        d.mkdir(parents=True, exist_ok=True)
        page = d / "a.md"
        page.write_text("# a\n\nstale\n", encoding="utf-8")
        os.utime(page, (old, old))

    started = mut.docs_generate_start(store_dir, repo_id="b", config_path=config_path)
    assert started["ok"] is True
    status = _await_finished(store_dir)

    assert status["documents_written"] == 2
    # The pre-existing pages are still there, so the count is a filter and not a
    # side effect of them having been removed.
    assert (store_dir / "docs" / "api" / "a.md").read_text(encoding="utf-8") == "# a\n\nstale\n"
    assert (store_dir / "docs" / "api" / "b.md").is_file()


@pytest.mark.parametrize("value", [
    # Every row is a shape one of the two id WRITERS can emit, not a shape that
    # merely looks plausible. `normalize_remote_url` builds `host/path` from the
    # origin remote and keeps the port, so `:` is in a real id;
    # `_fallback_repo_id` builds `<directory name>@<root commit>` for a repo with
    # no remote, so `@` is, and the directory-name half can hold a space or a
    # bracket. A tighter character class refuses ids the product produces, and the
    # Regenerate button on those repo panes would 400 where it used to work.
    # Every value here is SYNTHETIC on purpose. The deep-nested row exists to cover
    # a five-segment namespace, and an earlier draft used a real id read out of a
    # local store, which the publish-guard refused: this repo is public, so a real
    # id in a fixture is a private identifier in published history.
    "r", "team/app", "acme/teams/platform/bi/one-order", "a_b.c-d", "R2",
    "alpha@48409ae66487", "gitlab.example.com/acme/api",
    "gitlab.example.com:8443/acme/api", "my project@abc123def456", ".hidden",
])
def test_validate_repo_id_accepts_the_ids_the_product_actually_writes(value):
    assert mut.validate_repo_id(value) == value


@pytest.mark.parametrize("value", ["--llm=openai", "--max-symbols=1", "-r", "-",
                                   "../etc", "..", "a/../b", "/etc/passwd", "/a",
                                   "a\tb", "a\x00b", "", "   ", "a//b", "a/",
                                   "x" * 513, 123, ["a"], {"a": 1}, None, True])
def test_validate_repo_id_refuses_everything_that_is_not_one(value):
    """The child's argv is a trust boundary even on loopback.

    A leading ``-`` is the one that mattered: ``--llm=openai`` reached
    ``contextlake kb docs`` as a FLAG and turned on the model tier the spawn
    withholds, with a real outbound call. The rest of this list is why the check is
    more than a dash test -- ``../etc``, an absolute path, a control character and
    a non-string are all things a dash test accepts and no repo id ever is.
    """
    assert mut.validate_repo_id(value) is None


def test_a_stale_pidfile_does_not_wedge_the_route(tmp_path, monkeypatch):
    """A run whose process is gone must not hold the in-progress claim for good.

    This row exists because the O_CREAT|O_EXCL claim gave an old line a new job.
    A pidfile used to be overwritten by the next start, so a dead pid could never
    block anything; now the ONLY thing that frees the claim is the
    ``pf.unlink(missing_ok=True)`` in ``docs_generate_status``'s not-running
    branch. Remove it and the route refuses every run from then on, with nothing
    anywhere saying why.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path)
    pf = mut._docs_pidfile(store_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"pid": 999999999, "repo": None,
                              "started_at": time.time() - 3600}))

    result = mut.docs_generate_start(store_dir, config_path=config_path)

    assert result["ok"] is True
    assert json.loads(pf.read_text())["pid"] == result["pid"] != 999999999
    _await_finished(store_dir)


def test_the_count_is_scoped_to_the_documents_the_run_could_write(tmp_path, monkeypatch):
    """A run scoped to one repo is not credited with another writer's page.

    ``kb index`` now writes documents on every commit, so the docs tree has a second
    concurrent producer. Here a fleet page written by something else has its mtime
    pinned into the second the scoped run starts in: measured, the count read 3 for
    a run whose own log said it wrote 2, and the third file still held the other
    writer's content.

    ``cmd_docs`` writes the fleet page only for a run that covered every indexed
    repo, so a scoped run can only ever have written its two named pages.
    """
    import os

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path, repo_id="b")
    fleet = store_dir / "docs" / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    page = fleet / "design.md"
    page.write_text("# written by a different writer\n", encoding="utf-8")

    started = mut.docs_generate_start(store_dir, repo_id="b", config_path=config_path)
    assert started["ok"] is True
    # Pinned to the whole second the run started in, which is what the counter
    # floors to. Done after the start so the value is known.
    stamp = int(started["started_at"])
    os.utime(page, (stamp, stamp))
    status = _await_finished(store_dir)

    assert status["documents_written"] == 2
    assert page.read_text(encoding="utf-8") == "# written by a different writer\n"


def test_the_cutoff_still_filters_on_an_unscoped_run(tmp_path, monkeypatch):
    """An unscoped run counts the whole docs tree, so the mtime cutoff is the only
    filter left. A page for a repo the store no longer holds, backdated an hour, is
    not this run's output: the answer is 3, and a cutoff that does not filter reads
    4."""
    import os

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path, repo_id="r")
    api = store_dir / "docs" / "api"
    api.mkdir(parents=True, exist_ok=True)
    ghost = api / "gone.md"
    ghost.write_text("# gone\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(ghost, (old, old))

    started = mut.docs_generate_start(store_dir, repo_id=None, config_path=config_path)
    assert started["ok"] is True
    status = _await_finished(store_dir)

    assert status["documents_written"] == 3
    assert ghost.read_text(encoding="utf-8") == "# gone\n"


def test_a_start_is_refused_while_the_claim_is_held_but_holds_no_pid_yet(tmp_path,
                                                                        monkeypatch):
    """The claim window itself refuses a second run.

    An empty pidfile is what the winner leaves between taking the in-progress token
    and writing its pid into it. ``docs_generate_status`` cannot parse that and
    reports not-running, which is correct for a liveness question and wrong as a
    permission to spawn. Before the O_CREAT|O_EXCL claim, this state let a second
    request through: two children, one log file opened "w" twice, shredded output.

    PRECONDITION: the isolated HOME is load-bearing. If this guard is deleted the
    call spawns a real ``contextlake kb docs``, and the guard being deleted is the
    one stopping it.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path)
    pf = mut._docs_pidfile(store_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("")
    assert mut.docs_generate_status(store_dir)["running"] is False

    result = mut.docs_generate_start(store_dir, config_path=config_path)

    assert result["ok"] is False
    assert result["running"] is True
    assert "already in progress" in result["error"]
    assert not (store_dir / "dashboard" / "docs-gen.log").exists()


def test_a_failed_spawn_releases_the_claim_instead_of_wedging_the_route(tmp_path,
                                                                       monkeypatch):
    """A spawn that raises must hand the in-progress token back.

    The token is a file, and ``docs_generate_status`` deliberately does not delete
    an unparseable one (that state is the claim mid-write). So a spawn that failed
    after claiming would leave an empty file that refuses every later run for good,
    with no error anywhere saying why -- fixable only by deleting it by hand.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    store_dir, config_path = _docs_store(tmp_path)

    def boom(*_a, **_kw):
        raise OSError("no fork for you")

    monkeypatch.setattr(mut.subprocess, "Popen", boom)
    with pytest.raises(OSError):
        mut.docs_generate_start(store_dir, config_path=config_path)
    assert not mut._docs_pidfile(store_dir).exists()

    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    result = mut.docs_generate_start(store_dir, config_path=config_path)
    assert result["ok"] is True
    _await_finished(store_dir)
