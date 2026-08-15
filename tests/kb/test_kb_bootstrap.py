"""Tests for the bootstrap orchestrator (stage sequencing + skip flags)."""

from argparse import Namespace

import pytest

import contextlake.cli as cli
import contextlake.kb.commands as kb
from contextlake import core

_CORE = ["fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
         "switch_repository_branches", "verify_structure"]


def _kb_stages_referenced_by_cli() -> list[str]:
    """Every ``kb.cmd_*`` the CLI can put in bootstrap's stage list, read from source.

    This list used to be written out by hand, and adding a stage silently made these
    tests run the REAL command instead of a stub: the stage tried to open a store at
    the sentinel config path, failed, and bootstrap reported a failed stage. A hand-kept
    mirror of a list that lives somewhere else goes stale in exactly one direction, and
    the failure looks like a product bug rather than a stale test.

    Both `_bootstrap` (which builds the stage list) and the module-level stage helpers
    (which close over the same `kb` module) are scanned, since a stage can be wired from
    either.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cli))
    names = {node.attr
             for node in ast.walk(tree)
             if isinstance(node, ast.Attribute)
             and isinstance(node.value, ast.Name) and node.value.id == "kb"
             and node.attr.startswith("cmd_")}
    return sorted(names)


# Stubbing is driven by the SWEEP, so a newly wired stage is stubbed the day it lands.
_KB = _kb_stages_referenced_by_cli()

# Running ORDER is a contract these tests exist to pin, and a sorted sweep cannot state
# it, so the sequence stays written out. The two check each other: the sweep cannot miss
# a stage, and this list cannot silently drift from it.
_KB_ORDER = ["cmd_index", "cmd_connect", "cmd_embed", "cmd_enrich", "cmd_wiki",
             "cmd_graph", "cmd_steer"]

assert set(_KB_ORDER) == set(_KB), (
    f"bootstrap's kb stages changed: cli.py wires {sorted(_KB)}, this file expects "
    f"{sorted(_KB_ORDER)}. Put the new stage in _KB_ORDER at the position it runs.")

# The sweep is only worth anything if it can actually see stages. Without this, a rename
# of the `kb` alias would empty it, every stub would silently not be installed, and each
# test below would pass while exercising nothing it claims to.
assert {"cmd_index", "cmd_wiki", "cmd_steer"} <= set(_KB), (
    f"the stage sweep found {_KB}, which is missing stages the CLI definitely wires; "
    f"it is not reading cli.py the way it thinks it is")


def _core_return(name):
    """fetch hands back the project map the later stages consume; the rest hand back
    a StageResult, which is what bootstrap now scores its mirror stage on."""
    return {"grp/a": {}} if name == "fetch_gitlab_projects" else core.StageResult()


def _stub_core(monkeypatch, calls=None):
    for name in _CORE:
        def _stub(*a, _n=name, **k):
            if calls is not None:
                calls.append(_n)
            return _core_return(_n)

        monkeypatch.setattr(cli, name, _stub)


def _record(monkeypatch):
    calls = []
    _stub_core(monkeypatch, calls)
    for name in _KB:
        monkeypatch.setattr(kb, name, lambda a, _n=name: (calls.append(_n), 0)[1])
    return calls


def _args(**over):
    base = dict(no_sync=False, no_connect=False, no_embed=False, no_enrich=False,
                no_wiki=False, no_diagrams=False, kb_config=None, config=None,
                workspace=None, source=None, out=None)
    base.update(over)
    return Namespace(**base)


def test_bootstrap_runs_every_stage_in_order(monkeypatch, tmp_path):
    calls = _record(monkeypatch)
    cli._bootstrap(_args(), {}, str(tmp_path), "grp")
    assert calls == _CORE + _KB_ORDER


def test_bootstrap_skip_flags(monkeypatch, tmp_path):
    calls = _record(monkeypatch)
    cli._bootstrap(_args(no_sync=True, no_connect=True, no_embed=True, no_enrich=True,
                         no_wiki=True, no_diagrams=True), {}, str(tmp_path), "grp")
    # no sync; only the always-on kb stages: index + steer. Every skippable stage has
    # its flag passed here, so a new stage that forgets to honour its own flag fails.
    assert calls == ["cmd_index", "cmd_steer"]


def test_bootstrap_no_enrich_flag_omits_only_enrich(monkeypatch, tmp_path):
    calls = _record(monkeypatch)
    cli._bootstrap(_args(no_enrich=True), {}, str(tmp_path), "grp")
    assert calls == _CORE + ["cmd_index", "cmd_connect", "cmd_embed", "cmd_wiki",
                              "cmd_graph", "cmd_steer"]


def test_bootstrap_enrich_runs_between_embed_and_wiki(monkeypatch, tmp_path):
    calls = _record(monkeypatch)
    cli._bootstrap(_args(), {}, str(tmp_path), "grp")
    assert calls.index("cmd_embed") < calls.index("cmd_enrich") < calls.index("cmd_wiki")


def test_bootstrap_enrich_targets_all_workspace_repos(monkeypatch, tmp_path):
    """Defensive: reset kb_args.args to [] for enrich, even though bootstrap has
    no positional args and _connect_targets short-circuits on workspace before
    consulting args - belt-and-suspenders, not a currently reachable bug."""
    seen = {}
    _stub_core(monkeypatch)
    for name in _KB:
        monkeypatch.setattr(kb, name, lambda a, _n=name: (seen.__setitem__(_n, a), 0)[1])
    cli._bootstrap(_args(args=["stray-repo-filter"]), {}, str(tmp_path), "grp")
    assert seen["cmd_enrich"].args == []


def test_bootstrap_continues_past_a_failing_stage_then_exits_nonzero(monkeypatch, tmp_path):
    calls = _record(monkeypatch)

    def boom(a):
        raise RuntimeError("connect blew up")

    monkeypatch.setattr(kb, "cmd_connect", boom)
    # a non-foundational stage failing must not abort the run -- the stages after it
    # still run -- but bootstrap must NOT report a hollow success: it exits non-zero.
    with pytest.raises(SystemExit) as exc:
        cli._bootstrap(_args(no_sync=True, no_embed=True, no_enrich=True, no_wiki=True),
                       {}, str(tmp_path), "grp")
    assert exc.value.code == 1
    assert calls == ["cmd_index", "cmd_graph", "cmd_steer"]


def test_bootstrap_records_a_failed_mirror_stage_and_exits_nonzero(monkeypatch, tmp_path):
    """A mirror that failed to clone is a failed stage, exactly like a failing kb
    stage: recorded and exited on, but never allowed to abort the rest -- the
    knowledge layer still gets built from the repos that did land."""
    calls = _record(monkeypatch)
    monkeypatch.setattr(cli, "clone_missing_repos",
                        lambda *a, **k: (calls.append("clone_missing_repos"),
                                         core.StageResult(failed=1))[1])
    with pytest.raises(SystemExit) as exc:
        cli._bootstrap(_args(), {}, str(tmp_path), "grp")
    assert exc.value.code == 1
    assert calls == _CORE + _KB_ORDER   # every stage still ran, in order


def test_bootstrap_exit_zero_on_partial_forgives_a_failed_mirror_stage(monkeypatch, tmp_path):
    _record(monkeypatch)
    monkeypatch.setattr(cli, "clone_missing_repos", lambda *a, **k: core.StageResult(failed=1))
    cli._bootstrap(_args(exit_zero_on_partial=True), {}, str(tmp_path), "grp")


def test_bootstrap_exits_nonzero_when_a_stage_returns_failure(monkeypatch, tmp_path):
    """A stage that returns a non-zero code (not just raising) is a failure too."""
    calls = _record(monkeypatch)
    monkeypatch.setattr(kb, "cmd_wiki", lambda a: (calls.append("cmd_wiki"), 1)[1])
    args = _args(no_sync=True, no_connect=True, no_embed=True, no_enrich=True)
    with pytest.raises(SystemExit) as exc:
        cli._bootstrap(args, {}, str(tmp_path), "grp")
    assert exc.value.code == 1
    assert calls == ["cmd_index", "cmd_wiki", "cmd_graph", "cmd_steer"]


def test_bootstrap_aborts_when_index_fails(monkeypatch, tmp_path):
    """The code graph is foundational: if index fails, stop immediately (non-zero)
    and do not run connect/embed/wiki/steer on an absent graph."""
    calls = _record(monkeypatch)
    monkeypatch.setattr(kb, "cmd_index", lambda a: (calls.append("cmd_index"), 1)[1])
    with pytest.raises(SystemExit) as exc:
        cli._bootstrap(_args(no_sync=True), {}, str(tmp_path), "grp")
    assert exc.value.code == 1
    assert calls == ["cmd_index"]   # nothing downstream ran


def test_bootstrap_honors_explicit_workspace(monkeypatch, tmp_path):
    """--workspace must win over the mirror's work_dir (it used to be silently
    ignored), and the steering files follow it."""
    seen = {}
    _stub_core(monkeypatch)
    for name in _KB:
        monkeypatch.setattr(kb, name, lambda a, _n=name: (seen.__setitem__(_n, a), 0)[1])
    elsewhere = tmp_path / "elsewhere"
    cli._bootstrap(_args(no_sync=True, workspace=str(elsewhere)), {}, str(tmp_path), "grp")
    assert seen["cmd_index"].workspace == str(elsewhere)
    assert seen["cmd_steer"].out == str(elsewhere)


def test_bootstrap_passes_kb_config_not_sync_config(monkeypatch, tmp_path):
    seen = {}
    _stub_core(monkeypatch)
    for name in _KB:
        monkeypatch.setattr(kb, name, lambda a, _n=name: (seen.__setitem__(_n, a), 0)[1])
    cli._bootstrap(_args(no_sync=True, config="/sync.ini", kb_config="/kb.toml"),
                   {}, str(tmp_path), "grp")
    # kb stages get the kb.toml + the workspace, never the sync INI
    assert seen["cmd_index"].config == "/kb.toml"
    assert seen["cmd_index"].workspace == str(tmp_path)
    assert seen["cmd_steer"].out == str(tmp_path)
