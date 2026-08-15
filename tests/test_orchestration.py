"""Integration-level tests for the orchestration verbs and CLI dispatch."""

import json
import re

import pytest

from conftest import make_local_repo
from contextlake import cli, core

PROJECTS = {
    "g/a": {"archived": False, "http": "ha", "ssh": "sa", "default_branch": "main"},
    "g/b": {"archived": False, "http": "hb", "ssh": "sb", "default_branch": "main"},
    "g/old": {"archived": True, "http": "ho", "ssh": "so", "default_branch": "main"},
}

_FAKE_CFG = {"work_dir": "/tmp/x", "gitlab_group": "g"}


def _patch_config(monkeypatch):
    # **kw so this stub keeps accepting whatever load_config grows. It previously
    # took only `path`, so adding the `cli_group` argument -- which exists to stop a
    # false "no group found" warning on every --group invocation -- broke four tests
    # that care about dispatch and nothing about config.
    monkeypatch.setattr(cli, "load_config", lambda path=None, **kw: dict(_FAKE_CFG))


def _stage_return(name):
    """What a stubbed pipeline stage has to hand back for cli.main() to score the
    run: fetch still returns the project map every later stage consumes, the rest
    return a StageResult. Returning None here would make the CLI's exit-code
    aggregation blow up -- deliberately, since a stage that reports nothing is the
    silent failure the result type was added to end."""
    return dict(PROJECTS) if name == "fetch_gitlab_projects" else core.StageResult()


def _stub_stages(monkeypatch, names, record=None):
    """Replace each named cli-level stage with a stub that records its name."""
    for name in names:
        def _stub(*a, _n=name, **k):
            if record is not None:
                record.append(_n)
            return _stage_return(_n)

        monkeypatch.setattr(cli, name, _stub)


_PIPELINE = ["fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
             "switch_repository_branches", "verify_structure"]


@pytest.fixture
def cached_projects(monkeypatch):
    monkeypatch.setattr(core, "load_gitlab_projects", lambda config, group: dict(PROJECTS))


def test_clone_missing_dry_run(tmp_path, base_config, fake_subprocess, cached_projects, gls_logs):
    cfg = base_config.copy()
    cfg["dry_run"] = "true"
    core.clone_missing_repos(str(tmp_path), cfg, "g")
    # archived repo excluded; nothing actually cloned in dry-run
    assert not fake_subprocess.commands_matching("clone")
    assert "To clone: 2" in gls_logs.text


def test_clone_missing_skips_already_present(
    tmp_path, base_config, fake_subprocess, cached_projects
):
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "g" / "b" / ".git").mkdir(parents=True)
    core.clone_missing_repos(str(tmp_path), base_config, "g")
    assert not fake_subprocess.commands_matching("git", "clone")


def test_verify_structure_reports_nested_and_extra(tmp_path, base_config, monkeypatch, gls_logs):
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g, **kw: dict(PROJECTS))
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "g" / "a" / "inner" / ".git").mkdir(parents=True)  # nested
    (tmp_path / "g" / "extra" / ".git").mkdir(parents=True)  # not in GitLab
    core.verify_structure(str(tmp_path), base_config, "g")
    # H5: aligned kv summary (mirrors _status_summary's glyph/label/count rows)
    # replaces the old flat "Verification complete: ..." line.
    assert re.search(r"Nested\s+1\b", gls_logs.text)
    assert "g/a/inner" in gls_logs.text


def test_verify_structure_summary_emitted_per_line(tmp_path, base_config, monkeypatch, gls_logs):
    # HO-2: kv() must be logged one row per call, never a single multi-line
    # log() call, so each row gets its own timestamp/format.
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g, **kw: dict(PROJECTS))
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    core.verify_structure(str(tmp_path), base_config, "g")
    kv_lines = [rec.getMessage() for rec in gls_logs.records if "Valid" in rec.getMessage()]
    # The Valid row must arrive as its own single-line record: a single
    # log(kv(...)) call would deliver one record carrying the whole multi-line
    # block (newline present), which the next assertion rejects. (We check the
    # per-record shape, not an exact record count, since the shared-logger test
    # fixture can double-deliver a record depending on run order.)
    assert kv_lines
    assert "\n" not in kv_lines[0]
    assert re.search(r"Valid\s+1\b", kv_lines[0])
    assert re.search(r"Missing\s+2\b", gls_logs.text)


def test_show_status_counts(tmp_path, base_config, monkeypatch, gls_logs):
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g, **kw: dict(PROJECTS))
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    core.show_status(str(tmp_path), base_config, "g")
    # styled, right-aligned summary: "<glyph> Synchronized   1"
    assert re.search(r"Synchronized\s+1\b", gls_logs.text)
    assert re.search(r"Missing\s+1\b", gls_logs.text)  # g/b missing (g/old archived)


@pytest.mark.parametrize(
    "command,target",
    [
        ("fetch", "fetch_gitlab_projects"),
        ("clone", "clone_missing_repos"),
        ("update", "update_repositories"),
        ("branches", "switch_repository_branches"),
        ("verify", "verify_structure"),
        ("status", "show_status"),
    ],
)
def test_main_dispatches_to_command(monkeypatch, command, target):
    called = {"n": 0}
    _patch_config(monkeypatch)

    def _stub(*a, **k):
        called["n"] += 1
        return _stage_return(target)

    monkeypatch.setattr(cli, target, _stub)
    cli.main(["mirror", command])
    assert called["n"] == 1


def test_main_sync_runs_full_pipeline(monkeypatch, capsys):
    order = []
    _stub_stages(monkeypatch, _PIPELINE, record=order)
    monkeypatch.setattr(cli, "run_audit", lambda *a, **k: None)
    _patch_config(monkeypatch)
    cli.main(["mirror", "sync"])
    assert order == [
        "fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
        "switch_repository_branches", "verify_structure",
    ]
    # H4: glyph-prefixed finale, exclamation softened to match the other summaries.
    # cli.main() rebuilds the logger's handlers via setup_logging(), so gls_logs
    # (which attaches to the handler that existed before the call) misses this
    # output -- capsys reads real stdout instead, so it still sees it.
    out = capsys.readouterr().out
    assert "✓ Full synchronization complete" in out
    assert "Full synchronization complete!" not in out
    # M1: sync gets the same ▶-prefixed phase header bootstrap uses for the
    # equivalent stage, so the two commands read as one consistent system.
    assert "▶ Mirror repositories from GitLab" in out


def test_main_sync_headers_audit_stage_when_enabled(monkeypatch, capsys):
    _stub_stages(monkeypatch, _PIPELINE)
    monkeypatch.setattr(cli, "run_audit", lambda *a, **k: None)
    _patch_config(monkeypatch)
    cli.main(["mirror", "sync"])
    out = capsys.readouterr().out
    assert "▶ Audit repositories (health & age)" in out


def test_main_sync_skips_audit_header_with_no_audit(monkeypatch, capsys):
    _stub_stages(monkeypatch, _PIPELINE)
    run_audit_calls = []
    monkeypatch.setattr(cli, "run_audit", lambda *a, **k: run_audit_calls.append(1))
    _patch_config(monkeypatch)
    cli.main(["mirror", "sync", "--no-audit"])
    out = capsys.readouterr().out
    assert "▶ Audit repositories (health & age)" not in out
    assert not run_audit_calls


class _SpyProgress:
    """Stand-in for style.Progress that only records call counts (no rendering),
    mirroring the wire-through idiom in tests/kb/test_kb_wiki.py and
    tests/kb/test_kb_commands.py."""

    instances: list["_SpyProgress"] = []

    def __init__(self, total, **kwargs):
        self.total = total
        self.label = kwargs.get("label")
        self.advance_calls = 0
        self.done_calls = 0
        _SpyProgress.instances.append(self)

    def advance(self, *args, **kwargs):
        self.advance_calls += 1

    def done(self, *args, **kwargs):
        self.done_calls += 1


def test_update_repositories_reports_progress_and_leaves_stdout_unchanged(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """Wire-through: Progress.advance fires once per repo across every _status
    branch (updated/nochange/skip alike) and done() once, on a separate channel
    (stderr) from the existing stdout `_status(...)` detail lines, which must
    render exactly as before (byte-identical: same counter/glyph/path/message).
    """
    for name in ("r1", "r2", "r3"):
        make_local_repo(tmp_path, name)

    outcomes = {
        "r1": ("ok", "r1", "Updated to abc123"),
        "r2": ("nochange", "r2", "Already up to date"),
        "r3": ("skip", "r3", "Skipped (unsafe: dirty)"),
    }
    monkeypatch.setattr(core, "update_repository", lambda p, wd, cfg: outcomes[p])

    _SpyProgress.instances = []
    monkeypatch.setattr(core.style, "Progress", _SpyProgress)

    core.update_repositories(str(tmp_path), base_config)

    assert len(_SpyProgress.instances) == 1
    p = _SpyProgress.instances[0]
    assert p.total == 3
    assert p.label == "update"
    assert p.advance_calls == 3
    assert p.done_calls == 1

    text = gls_logs.text
    for path, (_status_val, _p, message) in outcomes.items():
        assert path in text
        assert message in text
    # The existing counter text stays exactly as before: "[i/3]" for each repo.
    for i in range(1, 4):
        assert f"[{i}/3]" in text


def test_switch_repository_branches_reports_progress(
    tmp_path, base_config, monkeypatch, gls_logs, cached_projects
):
    """Same wire-through coverage for the branches loop: advance once per repo
    (switched/already/error branches), done() once, existing stdout unchanged.
    """
    for name in ("g/a", "g/b"):
        make_local_repo(tmp_path, name)

    outcomes = {
        "g/a": ("switched", "g/a", "Switched to dev"),
        "g/b": ("error", "g/b", "checkout failed"),
    }
    monkeypatch.setattr(core, "switch_repository_branch", lambda p, proj, wd, cfg: outcomes[p])

    _SpyProgress.instances = []
    monkeypatch.setattr(core.style, "Progress", _SpyProgress)

    core.switch_repository_branches(str(tmp_path), base_config, "g")

    assert len(_SpyProgress.instances) == 1
    p = _SpyProgress.instances[0]
    assert p.total == 2
    assert p.label == "branches"
    assert p.advance_calls == 2
    assert p.done_calls == 1

    text = gls_logs.text
    for path, (_status_val, _p, message) in outcomes.items():
        assert path in text
        assert message in text
    for i in range(1, 3):
        assert f"[{i}/2]" in text


def test_verify_honours_the_repos_filter_on_both_sides(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """Filtering only the local side would report every unmatched project as
    `missing`, so a scoped verify would look broken rather than scoped."""
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g, **kw: dict(PROJECTS))
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "g" / "b" / ".git").mkdir(parents=True)

    cfg = base_config.copy()
    cfg["repo_filter"] = "g/a"
    result = core.verify_structure(str(tmp_path), cfg, "g")

    assert result.ok == 1  # only g/a considered
    assert re.search(r"Missing\s+0\b", gls_logs.text)
    assert re.search(r"Extra\s+0\b", gls_logs.text)


def test_status_on_a_cold_cache_reports_instead_of_enumerating(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """`status` reads as an inspection and is the first command of the day. On a
    cold cache it silently enumerated the whole forge and wrote the cache, which
    can take 30-50s and can fail on the network."""
    called = []
    monkeypatch.setattr(core, "fetch_gitlab_projects",
                        lambda g, c: called.append(g) or {})
    cfg = base_config.copy()
    cfg.update(cache_dir=str(tmp_path / "cache"), cache_json="p.json", cache_file="p.txt")

    core.show_status(str(tmp_path), cfg, "g")

    assert called == [], "status must not enumerate the forge"
    assert "run 'fetch' first" in gls_logs.text
    assert not (tmp_path / "cache" / "p.json").exists(), "status must not write the cache"


def test_a_filter_that_matches_nothing_locally_says_so(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """Honouring --repos makes "matched nothing" reachable, and a typo must not
    read as a clean run over an empty set. fetch already warns for the same
    situation against the project list."""
    make_local_repo(tmp_path, "blog")
    monkeypatch.setattr(core, "update_repository",
                        lambda p, wd, cfg: ("nochange", p, "Already"))

    cfg = base_config.copy()
    cfg["repo_filter"] = "no-such-repo"
    core.update_repositories(str(tmp_path), cfg)

    assert "No local repositories matched" in gls_logs.text


def test_verify_on_a_cold_cache_is_read_only_as_its_help_promises(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """`mirror verify`'s own help says "(read-only)" and "change nothing", but a
    cold cache made it enumerate the forge and write the cache -- the same
    defect as status."""
    called = []
    monkeypatch.setattr(core, "fetch_gitlab_projects",
                        lambda g, c: called.append(g) or {})
    cfg = base_config.copy()
    cfg.update(cache_dir=str(tmp_path / "cache"), cache_json="p.json", cache_file="p.txt")

    core.verify_structure(str(tmp_path), cfg, "g")

    assert called == [], "verify must not enumerate the forge"
    assert "run 'fetch' first" in gls_logs.text
    assert not (tmp_path / "cache" / "p.json").exists()


def _write_scoped_cache(tmp_path, names, scope):
    """A warm project cache plus the sidecar recording the --repos that built it."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "p.json").write_text(json.dumps({
        n: {"full_path": f"g/{n}", "http": "h", "ssh": "s",
            "archived": False, "default_branch": "main"} for n in names}))
    (cache / "p.json.filter").write_text(scope)
    return {"cache_dir": str(cache), "cache_json": "p.json", "cache_file": "p.txt"}


def test_status_refuses_to_pass_off_another_filters_cache_as_the_group(
    tmp_path, base_config, gls_logs
):
    """`status` is the command the docs send you to before a sync. After any
    filtered run it reported that filter's count as the group total, with no
    mention that a filter had shaped the cache -- under-reporting a 40-repo group
    as 2 and looking completely healthy doing it."""
    cfg = base_config.copy()
    cfg.update(_write_scoped_cache(tmp_path, ("api", "web"), "api,web"))

    core.show_status(str(tmp_path), cfg, "g")

    text = gls_logs.text
    assert not re.search(r"GitLab projects \(active\)\s+2\b", text), \
        "a scoped count must never be presented as the group total"
    assert "covers only --repos 'api,web'" in text


def test_status_names_the_scope_when_it_is_reporting_one(tmp_path, base_config, gls_logs):
    """With the same --repos in force the cache does answer, and the counts are
    right -- but they still describe a subset, so the report has to say so."""
    cfg = base_config.copy()
    cfg.update(_write_scoped_cache(tmp_path, ("api", "web"), "api,web"))
    cfg["repo_filter"] = "api,web"

    core.show_status(str(tmp_path), cfg, "g")

    text = gls_logs.text
    assert "Scoped to --repos api,web" in text
    assert re.search(r"GitLab projects \(active\)\s+2\b", text)


def test_status_narrows_both_sides_of_the_comparison(tmp_path, base_config, gls_logs):
    """`status` compares the project list against the local tree, so a filter
    that narrows one side and not the other invents a difference: a fully-synced
    workspace reported every non-matching clone as an Extra repository. `verify`
    already narrows both; `status` did not."""
    cfg = base_config.copy()
    cfg.update(_write_scoped_cache(tmp_path, ("api", "web", "billing"), ""))
    cfg["repo_filter"] = "api"
    for name in ("api", "web", "billing"):
        make_local_repo(tmp_path, name)

    core.show_status(str(tmp_path), cfg, "g")

    text = gls_logs.text
    assert re.search(r"GitLab projects \(active\)\s+1\b", text)
    assert re.search(r"Local repositories\s+1\b", text)
    assert re.search(r"Synchronized\s+1\b", text)
    assert re.search(r"Extra\s+0\b", text)
    assert "Extra repositories" not in text


# --- multi-group workspaces --------------------------------------------------

_ALPHA_PROJECTS = {
    "team/api": {"archived": False, "http": "h", "ssh": "s", "default_branch": "main"},
}


def _clone_from(root, rel_path, origin):
    """A local clone carrying the origin remote a real `git clone` would record."""
    git = root / rel_path / ".git"
    git.mkdir(parents=True)
    (git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n"
        f'[remote "origin"]\n\turl = {origin}\n'
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    )


def _multi_group_workspace(tmp_path):
    """A workspace legitimately holding two groups. The local paths cannot tell
    them apart -- `to_local_path` strips the `<group>/` prefix -- so only the
    origin remote can."""
    _clone_from(tmp_path, "team/api", "https://example.test/alpha/team/api.git")
    _clone_from(tmp_path, "platform/beacon", "git@example.test:beta/platform/beacon.git")


def test_verify_does_not_call_another_group_an_extra_repo(tmp_path, base_config,
                                                          monkeypatch, gls_logs):
    """Syncing group alpha in a workspace that also holds group beta reported
    every beta clone as an Extra repository. A workspace holding several groups is
    a supported arrangement, so repos outside `--group` are out of scope, not
    anomalies."""
    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda c, g, **kw: dict(_ALPHA_PROJECTS))
    _multi_group_workspace(tmp_path)
    core.verify_structure(str(tmp_path), base_config, "alpha")

    assert re.search(r"Valid\s+1\b", gls_logs.text)
    assert re.search(r"Extra\s+0\b", gls_logs.text)
    assert re.search(r"Other groups\s+1\b", gls_logs.text)
    assert "platform/beacon" not in gls_logs.text     # never listed as an anomaly


def test_status_does_not_count_another_group_as_extra(tmp_path, base_config,
                                                      monkeypatch, gls_logs):
    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda c, g, **kw: dict(_ALPHA_PROJECTS))
    _multi_group_workspace(tmp_path)
    core.show_status(str(tmp_path), base_config, "alpha")

    assert re.search(r"Local repositories\s+1\b", gls_logs.text)
    assert re.search(r"Synchronized\s+1\b", gls_logs.text)
    assert re.search(r"Extra\s+0\b", gls_logs.text)
    assert re.search(r"Other groups\s+1\b", gls_logs.text)


def test_branches_pass_skips_repos_from_another_group(tmp_path, base_config,
                                                      monkeypatch, gls_logs):
    """The branch pass sent every other group's clone through a switch that could
    only ever answer "Not in GitLab list" -- fetching nothing, switching nothing,
    and printing an anomaly line per repo. It is not asked about them at all now."""
    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda c, g, **kw: dict(_ALPHA_PROJECTS))
    _multi_group_workspace(tmp_path)
    seen = []
    monkeypatch.setattr(core, "switch_repository_branch",
                        lambda p, projects, wd, cfg: seen.append(p) or ("ok", p, "Already on main"))

    core.switch_repository_branches(str(tmp_path), base_config, "alpha")

    assert seen == ["team/api"]
    assert "Not in GitLab list" not in gls_logs.text
    assert "belong to another group" in gls_logs.text


def test_a_clone_with_no_readable_origin_is_still_reported(tmp_path, base_config,
                                                           monkeypatch, gls_logs):
    """The scoping is one-sided on purpose. Only a positively-attributed foreign
    repo drops out; a stray clone whose origin cannot be read keeps being reported
    exactly as before, so narrowing the report never becomes suppressing it."""
    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda c, g, **kw: dict(_ALPHA_PROJECTS))
    _clone_from(tmp_path, "team/api", "https://example.test/alpha/team/api.git")
    (tmp_path / "mystery" / ".git").mkdir(parents=True)   # no config, no origin
    core.verify_structure(str(tmp_path), base_config, "alpha")

    assert re.search(r"Extra\s+1\b", gls_logs.text)
    assert "mystery" in gls_logs.text


def test_nested_detection_still_sees_a_clone_from_another_group(tmp_path, base_config,
                                                                monkeypatch, gls_logs):
    """Group scoping must not reach the nested-repo check. A clone from another
    group sitting inside this group's working tree is exactly the corruption that
    check exists to catch, so narrowing its input would blind `verify` to the case
    that most needs reporting."""
    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda c, g, **kw: dict(_ALPHA_PROJECTS))
    _clone_from(tmp_path, "team/api", "https://example.test/alpha/team/api.git")
    _clone_from(tmp_path, "team/api/vendored",
                "git@example.test:beta/platform/beacon.git")
    core.verify_structure(str(tmp_path), base_config, "alpha")

    assert re.search(r"Nested\s+1\b", gls_logs.text)
    assert "team/api/vendored" in gls_logs.text
    assert re.search(r"Extra\s+0\b", gls_logs.text)     # still not an Extra repo


def test_mirror_banner_names_the_configured_forge(monkeypatch, capsys):
    """A GitHub run said "Mirror repositories from GitLab" two lines above "Enumerating
    via the GitHub REST API", which reads as "did it use the wrong platform?" in the
    first minute of a tool whose pitch is precision.

    The default-platform case is asserted by the order test above, and it passes whether
    or not the label is derived, since the default IS gitlab. This is the case that can
    only pass if it is derived.
    """
    _stub_stages(monkeypatch, _PIPELINE)
    monkeypatch.setattr(cli, "run_audit", lambda *a, **k: None)
    # Same shape as _patch_config, with the one field this test is about.
    monkeypatch.setattr(cli, "load_config",
                        lambda path=None, **kw: dict(_FAKE_CFG, platform="github"))
    cli.main(["mirror", "sync"])
    out = capsys.readouterr().out
    assert "Mirror repositories from GitHub" in out, (
        f"the mirror banner did not name the configured forge:\n{out[:600]}")
    assert "Mirror repositories from GitLab" not in out
