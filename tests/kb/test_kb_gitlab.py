"""Tests for the GitLab connector (MRs/issues) and its connect integration."""

from argparse import Namespace

import gitlab_sync.kb.connectors.orchestrate as orch
from gitlab_sync.kb.commands import cmd_connect
from gitlab_sync.kb.connectors.gitlab import GitLabConnector, associate_gitlab
from gitlab_sync.kb.model import Confidence, Repo
from gitlab_sync.kb.state import check_schema
from gitlab_sync.kb.store.sqlite_store import SqliteStore


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


# --- connect integration (no association rules needed for gitlab) ----------

class _StubGL:
    name = "gl"

    def fetch(self, repo_id):
        return ([{"iid": 1, "title": "MR", "state": "opened", "web_url": "u"}],
                [{"iid": 2, "title": "Issue", "state": "opened", "web_url": "u"}])


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
