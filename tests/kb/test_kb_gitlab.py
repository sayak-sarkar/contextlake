"""Tests for the GitLab connector (MRs/issues) and its connect integration."""

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import contextlake.kb.connectors.orchestrate as orch
from contextlake.kb.commands import cmd_connect
from contextlake.kb.connectors.gitlab import (
    NOT_GITLAB_HOSTS,
    GitLabConnector,
    associate_gitlab,
    match_files_to_nodes,
    normalize_host,
    skip_advice,
)
from contextlake.kb.model import Confidence, Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore


class _FakeGlab:
    def __init__(self):
        self.calls = []
        self.hosts = []

    def __call__(self, host, endpoint):
        self.hosts.append(host)
        self.calls.append(endpoint)
        if "merge_requests" in endpoint:
            return [{"iid": 7, "title": "Add X", "state": "opened", "web_url": "https://gl/mr/7"}]
        return [{"iid": 3, "title": "Fix Y", "state": "opened", "web_url": "https://gl/i/3"}]


# --- connector + endpoints -------------------------------------------------

# A repo id is canonical `host/namespace/project` -- what repo_identity.normalize_id
# actually produces. These tests previously used bare `api/svc`, a form the system
# stopped emitting, so they kept passing while every real call 404'd: the fixture and
# the code were wrong in the same direction. Derived from the real producer below so
# they cannot drift apart again.
def test_repo_id_fixture_matches_what_the_producer_emits():
    from contextlake.kb.repo_identity import normalize_remote_url
    assert normalize_remote_url("https://gitlab.com/team/api.git") == "gitlab.com/team/api"
    assert normalize_remote_url("git@gitlab.com:team/api.git") == "gitlab.com/team/api"


def test_fetch_strips_the_host_segment_from_the_repo_id():
    fake = _FakeGlab()
    mrs, issues = GitLabConnector("gl", group="team", runner=fake).fetch("gitlab.com/team/api")
    assert mrs[0]["iid"] == 7 and issues[0]["iid"] == 3
    # `team%2Fapi`, not `team%2Fgitlab.com%2Fteam%2Fapi`: the group is already in
    # the id, so prepending it is what produced a guaranteed 404.
    assert "projects/team%2Fapi/" in fake.calls[0] and "merge_requests" in fake.calls[0]
    assert "issues" in fake.calls[1] and "state=opened" in fake.calls[1]


def test_fetch_without_group_uses_the_full_namespace_path():
    fake = _FakeGlab()
    GitLabConnector("gl", runner=fake).fetch("gitlab.com/solo/repo")
    assert "projects/solo%2Frepo/" in fake.calls[0]


def test_fetch_handles_a_nested_namespace_on_a_self_hosted_host():
    fake = _FakeGlab()
    GitLabConnector("gl", hosts=["gitlab.example.internal"], runner=fake).fetch(
        "gitlab.example.internal/team/sub/proj")
    assert "projects/team%2Fsub%2Fproj/" in fake.calls[0]
    assert fake.hosts == ["gitlab.example.internal"] * 2


def test_group_filters_rather_than_prefixes():
    """A repo outside the configured group is skipped, not requested."""
    fake = _FakeGlab()
    mrs, issues = GitLabConnector("gl", group="team", runner=fake).fetch("gitlab.com/other/api")
    assert (mrs, issues) == ([], [])
    assert fake.calls == []


def test_remoteless_repo_id_makes_no_request():
    """The `name@root-commit` fallback names no GitLab project, so asking is pointless."""
    fake = _FakeGlab()
    mrs, issues = GitLabConnector("gl", runner=fake).fetch("myrepo@abc123def456")
    assert (mrs, issues) == ([], [])
    assert fake.calls == []


# --- host allowlist ---------------------------------------------------------
#
# A repo id names its host, and the host decides where `glab` sends the call. Only a host
# listed in `gitlab_hosts` is used, and the listed host is what reaches `glab`.

@pytest.fixture
def no_gitlab_host(monkeypatch):
    monkeypatch.delenv("GITLAB_HOST", raising=False)


def test_the_default_host_is_gitlab_com(no_gitlab_host):
    fake = _FakeGlab()
    conn = GitLabConnector("gl", runner=fake)
    assert conn.hosts == ("gitlab.com",)
    conn.fetch("gitlab.com/team/api")
    assert fake.hosts == ["gitlab.com", "gitlab.com"]


def test_a_repo_on_another_host_makes_no_request_by_default(no_gitlab_host):
    fake = _FakeGlab()
    assert GitLabConnector("gl", runner=fake).fetch("git.internal.test/team/api") == ([], [])
    assert fake.calls == []


def test_gitlab_host_in_the_environment_is_the_default_list(monkeypatch):
    monkeypatch.setenv("GITLAB_HOST", "https://Git.Corp:8443/")
    fake = _FakeGlab()
    conn = GitLabConnector("gl", runner=fake)
    assert conn.hosts == ("git.corp:8443",)
    conn.fetch("git.corp:8443/ns/p")
    assert fake.hosts == ["git.corp:8443"] * 2
    # gitlab.com is no longer allowed once GITLAB_HOST names another host.
    assert conn.fetch("gitlab.com/ns/p") == ([], [])
    assert len(fake.calls) == 2


def test_the_source_key_wins_over_gitlab_host(monkeypatch):
    monkeypatch.setenv("GITLAB_HOST", "env.corp")
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=["key.corp"], runner=fake)
    assert conn.hosts == ("key.corp",)
    assert conn.fetch("env.corp/ns/p") == ([], [])
    assert fake.calls == []


@pytest.mark.parametrize("repo_id", [
    "evil-git.corp/ns/p",          # the listed name inside a longer one
    "git.corp.evil.test/ns/p",     # the listed name as a prefix of another host
    "notgit.corp/ns/p",
    "evil.test/git.corp/p",        # the listed name in the path, not the host
    "git.corp:8443/ns/p",          # same name, other port
])
def test_a_host_matches_whole_and_not_by_substring_or_suffix(repo_id):
    fake = _FakeGlab()
    assert GitLabConnector("gl", hosts=["git.corp"], runner=fake).fetch(repo_id) == ([], [])
    assert fake.calls == []


def test_a_host_matches_without_regard_to_case():
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=["Git.CORP"], runner=fake)
    conn.fetch("GIT.corp/ns/p")
    # The listed host reaches the runner in lower case.
    assert fake.hosts == ["git.corp"] * 2


def test_the_port_is_part_of_the_host():
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=["git.corp:8443"], runner=fake)
    conn.fetch("git.corp:8443/ns/p")
    assert fake.hosts == ["git.corp:8443"] * 2
    # An entry with a port does not match the same name without one.
    assert conn.fetch("git.corp/ns/p") == ([], [])
    assert len(fake.calls) == 2


def test_the_runner_gets_the_listed_host_not_the_text_of_the_id():
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=["https://git.corp/"], runner=fake)
    conn.fetch("GIT.Corp/ns/p")
    assert fake.hosts == ["git.corp"] * 2


def test_a_single_string_is_one_host():
    assert GitLabConnector("gl", hosts="git.corp").hosts == ("git.corp",)


def test_an_empty_list_allows_no_host(no_gitlab_host):
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=[], runner=fake)
    assert conn.fetch("gitlab.com/ns/p") == ([], [])
    assert fake.calls == []


@pytest.mark.parametrize("value", ["", "*.corp", "git corp", "git.corp;rm", "-flag", 7, None])
def test_an_entry_that_is_not_a_host_name_is_ignored_and_reported(value, gls_logs):
    conn = GitLabConnector("gl", hosts=["git.corp", value])
    assert conn.hosts == ("git.corp",)
    assert any("not a host name" in r.getMessage() for r in gls_logs.records)


def test_a_bad_gitlab_host_variable_allows_no_host_and_is_not_printed(monkeypatch, gls_logs):
    monkeypatch.setenv("GITLAB_HOST", "bad host name")
    conn = GitLabConnector("gl")
    assert conn.hosts == ()
    text = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "GITLAB_HOST" in text and "bad host name" not in text


def test_normalize_host():
    assert normalize_host("Git.Corp") == "git.corp"
    assert normalize_host(" https://git.corp:8443/ns/p ") == "git.corp:8443"
    assert normalize_host("http://user:pw@git.corp/") == "git.corp"
    assert normalize_host("[::1]:8443") == "[::1]:8443"
    for bad in ("", "  ", "a b", "*.corp", "git.corp:", "git.corp:port", None, 5):
        assert normalize_host(bad) is None


@pytest.mark.parametrize("value", [
    "git.corp:0", "git.corp:65536", "git.corp:99999", "[::1]:0", "[::1]:65536",
    "https://git.corp:0/ns/p",
])
def test_a_port_outside_1_to_65535_is_not_a_host(value):
    assert normalize_host(value) is None


@pytest.mark.parametrize("value", ["git.corp:1", "git.corp:65535", "[::1]:65535"])
def test_the_ends_of_the_port_range_are_hosts(value):
    assert normalize_host(value) == value


def test_an_entry_with_a_port_out_of_range_is_ignored_and_reported(gls_logs):
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=["git.corp:0", "git.corp:65536"], runner=fake)
    assert conn.hosts == ()
    assert sum("not a host name" in r.getMessage() for r in gls_logs.records) == 2
    assert conn.fetch("git.corp:0/ns/p") == ([], [])
    assert fake.calls == []


def test_a_skipped_host_is_logged_once_per_run_and_names_the_key(gls_logs):
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=["git.corp"], runner=fake)
    for repo in ("other.test/a/b", "other.test/c/d", "other.test/a/b"):
        conn.fetch(repo)
    lines = [r.getMessage() for r in gls_logs.records if "other.test" in r.getMessage()]
    assert len(lines) == 1
    assert "gitlab_hosts" in lines[0] and "git.corp" in lines[0]
    assert conn.skipped_hosts == {"other.test": {"other.test/a/b", "other.test/c/d"}}
    # A new run logs and counts again.
    conn.begin_run()
    assert conn.skipped_hosts == {}
    conn.fetch("other.test/a/b")
    lines = [r.getMessage() for r in gls_logs.records if "other.test" in r.getMessage()]
    assert len(lines) == 2


def test_skip_advice_is_conditional_on_the_host_running_gitlab():
    assert skip_advice(["git.b"]) == (
        "If git.b runs GitLab, add it to gitlab_hosts on that source to read its repos.")
    assert skip_advice(["git.b", "git.a"]).startswith(
        "For each of git.a, git.b that runs GitLab, add it to gitlab_hosts")
    assert skip_advice([]) == ""


@pytest.mark.parametrize("host", sorted(NOT_GITLAB_HOSTS))
def test_skip_advice_never_names_a_host_known_not_to_be_gitlab(host):
    assert skip_advice([host]) == ""
    # An ssh port on the clone does not turn it into a GitLab host.
    assert skip_advice([f"{host}:22"]) == ""
    # Nor does the trailing dot of a fully qualified name, with or without a port.
    assert skip_advice([f"{host}."]) == ""
    assert skip_advice([f"{host}.:22"]) == ""
    # Another host in the same list still gets its advice, without this one in it.
    advice = skip_advice([host, "git.b"])
    assert "git.b" in advice and host not in advice


def test_skip_advice_gives_none_for_text_that_is_not_a_valid_entry():
    assert skip_advice(["git.corp:0", "no good"]) == ""


def test_the_skip_log_line_does_not_advise_gitlab_hosts_for_github_com(gls_logs):
    conn = GitLabConnector("gl", hosts=["git.corp"], runner=_FakeGlab())
    conn.fetch("github.com/org/repo")
    conn.fetch("other.test/org/repo")
    by_host = {h: [r.getMessage() for r in gls_logs.records if f"repos on {h}" in r.getMessage()]
               for h in ("github.com", "other.test")}
    assert len(by_host["github.com"]) == 1 and "gitlab_hosts" in by_host["github.com"][0]
    assert "runs GitLab" not in by_host["github.com"][0]
    assert "add it" not in by_host["github.com"][0]
    assert "If other.test runs GitLab, add it to gitlab_hosts" in by_host["other.test"][0]


def test_the_skip_log_line_does_not_advise_gitlab_hosts_for_github_com_with_a_trailing_dot(
        gls_logs):
    conn = GitLabConnector("gl", hosts=["git.corp"], runner=_FakeGlab())
    conn.fetch("github.com./org/repo")
    # A set: until an earlier test has called setup_logging, the logger still propagates and
    # gls_logs sees each record twice.
    (line,) = {r.getMessage() for r in gls_logs.records if "repos on github.com." in r.getMessage()}
    assert "gitlab_hosts (git.corp)" in line
    assert "runs GitLab" not in line and "add it" not in line


def test_the_skip_log_line_gives_no_advice_for_a_host_another_source_lists(gls_logs):
    conn = GitLabConnector("gb", hosts=["git.b"], runner=_FakeGlab())
    conn.other_hosts = frozenset({"git.a"})
    conn.fetch("git.a/org/repo")
    conn.fetch("other.test/org/repo")
    line_a = [r.getMessage() for r in gls_logs.records if "repos on git.a" in r.getMessage()]
    line_o = [r.getMessage() for r in gls_logs.records if "repos on other.test" in r.getMessage()]
    assert len(line_a) == 1 and "gitlab_hosts (git.b)" in line_a[0]
    assert "runs GitLab" not in line_a[0] and "add it" not in line_a[0]
    assert "If other.test runs GitLab, add it to gitlab_hosts" in line_o[0]


def test_a_listed_host_is_recorded_as_read_and_begin_run_forgets_it():
    conn = GitLabConnector("gl", hosts=["git.corp"], runner=_FakeGlab())
    conn.fetch("git.corp/ns/p")
    conn.fetch("other.test/ns/p")
    assert conn.read_repos == {"git.corp/ns/p"}
    assert conn.skipped_hosts == {"other.test": {"other.test/ns/p"}}
    conn.begin_run()
    assert conn.read_repos == set()


def test_a_repo_outside_the_group_is_not_counted_as_a_host_skip():
    conn = GitLabConnector("gl", group="team", hosts=["git.corp"], runner=_FakeGlab())
    conn.fetch("other.test/elsewhere/api")
    assert conn.skipped_hosts == {}


def test_fetch_changes_checks_the_host_too():
    fake = _FakeGlab()
    conn = GitLabConnector("gl", hosts=["git.corp"], runner=fake)
    assert conn.fetch_changes("other.test/ns/p", "7") == []
    assert fake.calls == []
    conn.fetch_changes("git.corp/ns/p", "7")
    assert fake.hosts == ["git.corp"]
    assert fake.calls == ["projects/ns%2Fp/merge_requests/7/diffs"]


def test_glab_is_run_with_the_listed_host(monkeypatch):
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(stdout="[]")

    monkeypatch.setattr("subprocess.run", run)
    GitLabConnector("gl", hosts=["git.corp:8443"]).fetch("git.corp:8443/ns/sub/p")
    assert seen[0] == ["glab", "api", "--hostname", "git.corp:8443",
                       "projects/ns%2Fsub%2Fp/merge_requests?state=opened&per_page=50"]
    assert seen[1][:4] == ["glab", "api", "--hostname", "git.corp:8443"]
    reset_breakers()


def test_a_failing_host_does_not_stop_calls_to_another_host(monkeypatch):
    import subprocess

    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    bad, good = [], []

    def run(argv, **kw):
        if "bad.corp" in argv:
            bad.append(argv)
            raise subprocess.TimeoutExpired(argv, 30)
        good.append(argv)
        return SimpleNamespace(stdout="[]")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    conn = GitLabConnector("gl", hosts=["bad.corp", "good.corp"])
    for i in range(8):
        conn.fetch(f"bad.corp/ns/p{i}")
    # Two calls per repo were asked for. The breaker for bad.corp opened and refused the rest.
    assert 0 < len(bad) < 16
    conn.fetch("good.corp/ns/p")
    assert len(good) == 2, "the failures on one host stopped calls to the other"
    reset_breakers()


def test_fetch_changes_returns_file_paths(monkeypatch):
    connector = GitLabConnector("team/api")
    monkeypatch.setattr(connector, "_run", lambda host, path: [
        {"new_path": "pay.py"}, {"new_path": "payer.py"}, {"new_path": "unrelated.md"},
    ])
    files = connector.fetch_changes("gitlab.com/team/api", "42")
    assert files == ["pay.py", "payer.py", "unrelated.md"]


def test_fetch_changes_returns_empty_on_failure(monkeypatch):
    connector = GitLabConnector("team/api")
    monkeypatch.setattr(
        connector, "_run", lambda host, path: (_ for _ in ()).throw(RuntimeError("boom")))
    assert connector.fetch_changes("gitlab.com/team/api", "42") == []


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
        touch_edges = [e for e in edges if e.relation == "touched_by"]
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
        assert not [e for e in edges if e.relation == "touched_by"]
    finally:
        store.close()


# --- enrich_repo_gitlab: MR/issue nodes become embeddable -------------------

def test_enrich_repo_gitlab_embeds_nodes_when_embedder_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(
        orch, "_embed_documents",
        lambda vs, emb, part, nodes, texts, batch_size: calls.append((part, len(nodes))),
    )
    store = SqliteStore(":memory:")
    try:
        nodes, _ = orch.enrich_repo_gitlab(
            _StubGLWithChanges([]), "team/api", store,
            embedder="fake-embedder", vector_store="fake-vector-store")
        assert calls == [("@connect:team/api", len(nodes))]
    finally:
        store.close()


def test_enrich_repo_gitlab_skips_embedding_without_embedder_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(orch, "_embed_documents", lambda *a, **k: calls.append(1))
    store = SqliteStore(":memory:")
    try:
        orch.enrich_repo_gitlab(_StubGLWithChanges([]), "team/api", store)
    finally:
        store.close()
    assert calls == []  # no embedder/vector_store configured -> never called


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


class _Src:
    """A SourceCfg-shaped stand-in: connector options arrive as model extras."""

    def __init__(self, **extra):
        self.name = "gl"
        self.mcp = None
        self.model_extra = extra


def test_numeric_source_options_are_coerced_however_they_were_written():
    """`--set timeout=3` stores the string "3" (a plain KEY=VALUE split, and a
    hand-edited config can be quoted too). Left as a string it reached
    `subprocess.run(timeout="3")`, which raises TypeError on every call: zero
    requests made, and the run still reported success."""
    conn = orch.build_gitlab(_Src(timeout="3", per_page="10"))
    assert conn.timeout == 3.0
    assert conn.per_page == 10
    assert isinstance(conn.timeout, float)
    assert isinstance(conn.per_page, int)


def test_numeric_source_options_still_accept_real_numbers():
    conn = orch.build_gitlab(_Src(timeout=7, per_page=25))
    assert conn.timeout == 7.0
    assert conn.per_page == 25


def test_unparseable_numeric_option_falls_back_instead_of_failing_every_call(gls_logs):
    conn = orch.build_gitlab(_Src(timeout="banana"))
    assert conn.timeout == 30  # the documented default
    assert any("banana" in r.getMessage() for r in gls_logs.records)


class _DeadGL:
    """The same source after the network went away: every call yields nothing."""

    name = "gl"

    def fetch(self, repo_id):
        return ([], [])

    def fetch_changes(self, repo_id, mr_iid):
        return []


def test_a_fetchless_reconnect_does_not_strand_the_nodes_it_unlinked(tmp_path, monkeypatch):
    """Connect once with the network up, then again with it down.

    Connector edges live in the @connect partition and its nodes in (external),
    so the stale sweep removed the edges and left the nodes: an orphan that no
    traversal can reach and no code question can surface.
    """
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
        assert store.nodes_by_name("team/api!1"), "precondition: the MR node landed"
    finally:
        store.close()

    monkeypatch.setattr(orch, "build_gitlab", lambda src: _DeadGL())
    cmd_connect(Namespace(config=str(tmp_path / "kb.toml")))

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        assert not store.nodes_by_name("team/api!1"), "unlinked node left stranded"
        assert not store.nodes_by_name("team/api#2"), "unlinked node left stranded"
    finally:
        store.close()


# --- kb connect over repos on a host that is not listed ---------------------

_HOSTS_CFG = ('[kb]\nstore_dir = "{store}"\n\n[[sources]]\ntype = "gitlab"\nname = "gl"\n'
              'gitlab_hosts = ["git.corp"]\n')


def _store_with_repos(tmp_path, *repo_ids):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(_HOSTS_CFG.format(store=store_dir.as_posix()))
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    for i, repo_id in enumerate(repo_ids):
        s.upsert_repo(Repo(id=repo_id, path=str(tmp_path / f"repo{i}")))
    s.close()
    return store_dir


def _glab_calls(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(stdout="[]")

    monkeypatch.setattr("subprocess.run", run)
    return calls


def test_connect_says_so_when_every_repo_is_on_an_unlisted_host(tmp_path, monkeypatch, gls_logs):
    """A run that skipped every repo must not read like a quiet fleet: "0 links", exit 0."""
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_with_repos(tmp_path, "other.test/team/api", "elsewhere.test/team/web")
    calls = _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    assert calls == [], "a repo on an unlisted host reached glab"
    text = gls_logs.text
    assert "skipped all 2 repo(s)" in text
    assert "elsewhere.test (1)" in text and "other.test (1)" in text
    assert "gitlab_hosts" in text
    assert "⚠ Connect complete: 0 external link(s) stored" in text
    assert "✓ Connect complete" not in text


def test_connect_counts_only_the_repos_that_were_skipped(tmp_path, monkeypatch, gls_logs):
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_with_repos(tmp_path, "git.corp/team/api", "other.test/team/web")
    calls = _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    assert calls and all(argv[3] == "git.corp" for argv in calls)
    text = gls_logs.text
    assert "skipped 1 of 2 repo(s)" in text and "other.test (1)" in text


def test_a_repo_on_a_listed_host_leaves_the_summary_unchanged(tmp_path, monkeypatch, gls_logs):
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_with_repos(tmp_path, "git.corp/team/api")
    _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    assert "skipped" not in gls_logs.text
    assert "✓ Connect complete: 0 external link(s) stored" in gls_logs.text


def test_a_watch_loop_logs_each_skipped_host_again_on_every_pass(
        tmp_path, monkeypatch, gls_logs):
    """The connector is built once and reused by every pass, so its record of skipped
    hosts has to be cleared at the start of each one."""
    import contextlake.kb.cmds.connect as cmds
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_with_repos(tmp_path, "other.test/team/api")
    _glab_calls(monkeypatch)
    monkeypatch.setattr(cmds, "_watch_loop", lambda once, interval: (once(), once()))

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"), watch=True,
                                 interval=1)) == 0

    lines = [r.getMessage() for r in gls_logs.records
             if "skipping repos on other.test" in r.getMessage()]
    assert len(lines) == 2
    assert gls_logs.text.count("skipped all 1 repo(s)") == 2


def test_connect_clears_the_links_a_wrong_host_query_once_stored(tmp_path, monkeypatch):
    """Links stored from a query that went to the wrong host are removed on the next run
    once that repo's host is off the list. The skipped repo gives an empty answer, and a run
    where every source answers stores it. (A run where a source is unavailable keeps the
    previous links instead.)"""
    import contextlake.kb.connectors.orchestrate as orchmod
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _store_with_repos(tmp_path, "other.test/team/api")
    real_build = orchmod.build_gitlab
    monkeypatch.setattr(orchmod, "build_gitlab", lambda src: _StubGL())
    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        assert store.nodes_by_name("other.test/team/api!1"), "precondition: the MR node landed"
    finally:
        store.close()

    monkeypatch.setattr(orchmod, "build_gitlab", real_build)
    _glab_calls(monkeypatch)
    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        assert not store.nodes_by_name("other.test/team/api!1")
    finally:
        store.close()



# --- more than one GitLab source, and the advice in the connect warning ---------

def _two_sources(tmp_path, *repo_ids, group_a=None):
    """A store with these repos and two GitLab sources: `ga` lists git.a, `gb` lists git.b."""
    store_dir = _store_with_repos(tmp_path, *repo_ids)
    group = f'group = "{group_a}"\n' if group_a else ""
    (tmp_path / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n\n'
        f'[[sources]]\ntype = "gitlab"\nname = "ga"\n{group}gitlab_hosts = ["git.a"]\n\n'
        '[[sources]]\ntype = "gitlab"\nname = "gb"\ngitlab_hosts = ["git.b"]\n')
    return store_dir


def test_a_healthy_multi_host_config_does_not_warn(tmp_path, monkeypatch, gls_logs):
    """Each repo is read by the source that lists its host, so no repo went unread."""
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _two_sources(tmp_path, "git.a/team/api", "git.b/team/web")
    calls = _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    assert sorted(argv[3] for argv in calls) == ["git.a"] * 2 + ["git.b"] * 2
    text = gls_logs.text
    assert "✓ Connect complete" in text
    assert "⚠" not in text
    assert "skipped " not in text
    # Each source still logs the host it leaves to the other. The line must not advise
    # listing that host, because the other source already reads it.
    assert "skipping repos on git.a" in text and "skipping repos on git.b" in text
    assert "runs GitLab" not in text


def test_a_multi_host_config_counts_only_the_repos_no_source_read(
        tmp_path, monkeypatch, gls_logs):
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _two_sources(tmp_path, "git.a/team/api", "git.b/team/web", "other.test/team/x")
    _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    text = gls_logs.text
    assert "⚠ Connect complete" in text
    lines = [r.getMessage() for r in gls_logs.records if "skipped " in r.getMessage()]
    assert len(lines) == 2
    for name, line in zip(("ga", "gb"), sorted(lines), strict=True):
        assert f"gitlab source '{name}' skipped 1 of 3 repo(s)" in line
        assert "no other gitlab source read them" in line
        assert "other.test (1)" in line
        assert "git.a (" not in line and "git.b (" not in line


def test_the_connect_warning_gives_no_advice_for_a_host_another_source_lists(
        tmp_path, monkeypatch, gls_logs):
    """`ga` reads only the group `a`, so a git.a repo outside it is read by no source.
    `gb` counts it, but git.a is a host `ga` lists, so the warning does not tell the user
    to add git.a to `gb`."""
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _two_sources(tmp_path, "git.a/a/api", "git.a/zzz/lib", group_a="a")
    _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    (line,) = [r.getMessage() for r in gls_logs.records if "skipped " in r.getMessage()]
    assert "gitlab source 'gb' skipped 1 of 2 repo(s)" in line and "git.a (1)" in line
    assert "runs GitLab" not in line and "add it" not in line
    assert "runs GitLab" not in gls_logs.text


def test_the_connect_warning_gives_no_advice_for_github_com(tmp_path, monkeypatch, gls_logs):
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_with_repos(tmp_path, "git.corp/team/api", "github.com/org/repo")
    _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    (line,) = [r.getMessage() for r in gls_logs.records if "skipped " in r.getMessage()]
    assert "skipped 1 of 2 repo(s)" in line and "github.com (1)" in line
    assert "runs GitLab" not in line and "add it" not in line
    assert "runs GitLab" not in gls_logs.text


def test_the_connect_warning_gives_no_advice_for_github_com_with_a_trailing_dot(
        tmp_path, monkeypatch, gls_logs):
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_with_repos(tmp_path, "git.corp/team/api", "github.com./org/repo")
    _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    (line,) = {r.getMessage() for r in gls_logs.records if "skipped " in r.getMessage()}
    assert "skipped 1 of 2 repo(s)" in line and "github.com. (1)" in line
    assert "runs GitLab" not in line and "add it" not in line
    assert "runs GitLab" not in gls_logs.text


def test_the_connect_warning_advises_only_the_hosts_that_may_run_gitlab(
        tmp_path, monkeypatch, gls_logs):
    from contextlake.kb.resilience import reset_breakers

    reset_breakers()
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_with_repos(tmp_path, "github.com/org/repo", "other.test/team/web")
    _glab_calls(monkeypatch)

    assert cmd_connect(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    (line,) = [r.getMessage() for r in gls_logs.records if "skipped " in r.getMessage()]
    assert "github.com (1)" in line and "other.test (1)" in line
    assert "If other.test runs GitLab, add it to gitlab_hosts" in line
    assert "If github.com" not in line and "github.com runs" not in line


# --- the docs say what the code does with a host that carries a port --------------

@pytest.mark.parametrize("page", ["docs/document-sources.md", "docs/connecting-and-enriching.md"])
def test_the_docs_say_to_list_the_host_as_gitlab_serves_its_api(page):
    root = Path(__file__).resolve().parents[2]
    text = " ".join((root / page).read_text(encoding="utf-8").split())
    assert "ssh://git@git.example.com:2222/ns/p" in text or "port 2222" in text
    assert "git.example.com:2222" in text
    assert "list the host as GitLab serves its API" in text
