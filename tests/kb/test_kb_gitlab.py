"""Tests for the GitLab connector (MRs/issues) and its connect integration."""

from argparse import Namespace

import contextlake.kb.connectors.orchestrate as orch
from contextlake.kb.commands import cmd_connect
from contextlake.kb.connectors.gitlab import (
    GitLabConnector,
    associate_gitlab,
    match_files_to_nodes,
)
from contextlake.kb.model import Confidence, Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore


class _FakeGlab:
    def __init__(self):
        self.calls = []

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if "merge_requests" in endpoint:
            return [{"iid": 7, "title": "Add X", "state": "opened", "web_url": "https://gl/mr/7"}]
        return [{"iid": 3, "title": "Fix Y", "state": "opened", "web_url": "https://gl/i/3"}]


# --- connector + endpoints -------------------------------------------------

def test_fetch_builds_encoded_endpoints():
    fake = _FakeGlab()
    mrs, issues = GitLabConnector("gl", group="team", runner=fake).fetch("api/svc")
    assert mrs[0]["iid"] == 7 and issues[0]["iid"] == 3
    assert "team%2Fapi%2Fsvc" in fake.calls[0] and "merge_requests" in fake.calls[0]
    assert "issues" in fake.calls[1] and "state=opened" in fake.calls[1]


def test_fetch_without_group_uses_repo_id():
    fake = _FakeGlab()
    GitLabConnector("gl", runner=fake).fetch("solo/repo")
    assert "solo%2Frepo" in fake.calls[0]


def test_fetch_changes_returns_file_paths(monkeypatch):
    connector = GitLabConnector("team/api")
    monkeypatch.setattr(connector, "_run", lambda path: [
        {"new_path": "pay.py"}, {"new_path": "payer.py"}, {"new_path": "unrelated.md"},
    ])
    files = connector.fetch_changes("team/api", "42")
    assert files == ["pay.py", "payer.py", "unrelated.md"]


def test_fetch_changes_returns_empty_on_failure(monkeypatch):
    connector = GitLabConnector("team/api")
    monkeypatch.setattr(connector, "_run", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    assert connector.fetch_changes("team/api", "42") == []


def test_match_files_to_nodes_finds_existing_file_nodes():
    store = SqliteStore(":memory:")
    store.upsert_nodes("team/api", [
        Node(id="team_api_pay_py", repo="team/api", kind="file", name="pay.py", file="pay.py"),
        Node(id="team_api_other_py", repo="team/api", kind="file",
             name="other.py", file="other.py"),
    ])
    matches = match_files_to_nodes(store, "team/api", ["pay.py", "missing.py"])
    assert matches == [("team_api_pay_py", Confidence.EXTRACTED)]


# --- pure mapping ----------------------------------------------------------

def test_associate_gitlab():
    nodes, edges = associate_gitlab(
        "team/api",
        [{"iid": 7, "title": "Add X", "state": "opened", "web_url": "u1"}],
        [{"iid": 3, "title": "Fix Y", "state": "opened", "web_url": "u2"}],
    )
    assert {n.kind for n in nodes} == {"repo", "mr", "issue"}
    mr = next(n for n in nodes if n.kind == "mr")
    assert mr.name == "team/api!7"
    assert mr.attrs["title"] == "Add X" and mr.attrs["state"] == "opened"
    issue = next(n for n in nodes if n.kind == "issue")
    assert issue.name == "team/api#3"
    assert {e.relation for e in edges} == {"has_merge_request", "has_issue"}
    assert all(e.confidence == Confidence.EXTRACTED for e in edges)


def test_associate_gitlab_skips_idless_items():
    nodes, _ = associate_gitlab("r", [{"title": "no iid"}], [])
    assert {n.kind for n in nodes} == {"repo"}  # the malformed MR is skipped


# --- enrich_repo_gitlab: MR-to-touched-file wiring -------------------------

class _StubGLWithChanges:
    name = "gl"

    def __init__(self, files):
        self._files = files

    def fetch(self, repo_id):
        return ([{"iid": 7, "title": "Add X", "state": "opened", "web_url": "u"}], [])

    def fetch_changes(self, repo_id, mr_iid):
        assert mr_iid == "7"
        return self._files


def test_enrich_repo_gitlab_links_mr_to_touched_file_nodes():
    store = SqliteStore(":memory:")
    try:
        store.upsert_nodes("team/api", [
            Node(id="team_api_pay_py", repo="team/api", kind="file", name="pay.py", file="pay.py"),
        ])
        nodes, edges = orch.enrich_repo_gitlab(
            _StubGLWithChanges(["pay.py", "missing.py"]), "team/api", store)
        mr = next(n for n in nodes if n.kind == "mr")
        touch_edges = [e for e in edges if e.relation == "touches"]
        assert {e.src for e in touch_edges} == {"team_api_pay_py", "repo_team_api"}
        assert all(e.dst == mr.id for e in touch_edges)
        file_edge = next(e for e in touch_edges if e.src == "team_api_pay_py")
        assert file_edge.confidence == Confidence.EXTRACTED
    finally:
        store.close()


def test_enrich_repo_gitlab_no_file_matches_skips_touches_edges():
    store = SqliteStore(":memory:")
    try:
        nodes, edges = orch.enrich_repo_gitlab(
            _StubGLWithChanges(["missing.py"]), "team/api", store)
        assert {n.kind for n in nodes} == {"repo", "mr"}
        assert not [e for e in edges if e.relation == "touches"]
    finally:
        store.close()


# --- connect integration (no association rules needed for gitlab) ----------

class _StubGL:
    name = "gl"

    def fetch(self, repo_id):
        return ([{"iid": 1, "title": "MR", "state": "opened", "web_url": "u"}],
                [{"iid": 2, "title": "Issue", "state": "opened", "web_url": "u"}])

    def fetch_changes(self, repo_id, mr_iid):
        return []


_CFG = '[kb]\nstore_dir = "{store}"\n\n[[sources]]\ntype = "gitlab"\nname = "gl"\ngroup = "team"\n'


def test_cmd_connect_gitlab_without_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(_CFG.format(store=store_dir.as_posix()))
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_repo(Repo(id="team/api", path=str(tmp_path / "repo")))
    s.close()

    monkeypatch.setattr(orch, "build_gitlab", lambda src: _StubGL())
    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        check_schema(store)
        assert store.nodes_by_name("team/api!1")  # MR node landed
        assert store.nodes_by_name("team/api#2")  # issue node landed
    finally:
        store.close()
