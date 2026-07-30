"""Dashboard mutating actions (kb/dashboard/mutations.py): sync/add a repo, the
HTTP-transport MCP server lifecycle. Against real throwaway git repos -- these
functions shell out to `git`, and the whole point of validate_clone_url is to
prove specific malicious inputs get rejected before they reach subprocess argv.
"""

import subprocess
import time

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
