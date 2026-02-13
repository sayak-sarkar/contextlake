"""Tests for the bootstrap orchestrator (stage sequencing + skip flags)."""

from argparse import Namespace

import gitlab_sync.cli as cli
import gitlab_sync.kb.commands as kb

_CORE = ["fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
         "switch_repository_branches", "verify_structure"]
_KB = ["cmd_index", "cmd_connect", "cmd_embed", "cmd_wiki", "cmd_steer"]


def _record(monkeypatch):
    calls = []
    for name in _CORE:
        monkeypatch.setattr(cli, name, lambda *a, _n=name, **k: calls.append(_n))
    for name in _KB:
        monkeypatch.setattr(kb, name, lambda a, _n=name: (calls.append(_n), 0)[1])
    return calls


def _args(**over):
    base = dict(no_sync=False, no_connect=False, no_embed=False, no_wiki=False,
                kb_config=None, config=None, workspace=None, source=None, out=None)
    base.update(over)
    return Namespace(**base)


def test_bootstrap_runs_every_stage_in_order(monkeypatch, tmp_path):
    calls = _record(monkeypatch)
    cli._bootstrap(_args(), {}, str(tmp_path), "grp")
    assert calls == _CORE + _KB


def test_bootstrap_skip_flags(monkeypatch, tmp_path):
    calls = _record(monkeypatch)
    cli._bootstrap(_args(no_sync=True, no_connect=True, no_embed=True, no_wiki=True),
                   {}, str(tmp_path), "grp")
    # no sync; only the always-on kb stages: index + steer
    assert calls == ["cmd_index", "cmd_steer"]


def test_bootstrap_continues_past_a_failing_stage(monkeypatch, tmp_path):
    calls = _record(monkeypatch)

    def boom(a):
        raise RuntimeError("connect blew up")

    monkeypatch.setattr(kb, "cmd_connect", boom)
    cli._bootstrap(_args(no_sync=True, no_embed=True, no_wiki=True), {}, str(tmp_path), "grp")
    # connect failed but index + steer still ran
    assert calls == ["cmd_index", "cmd_steer"]


def test_bootstrap_passes_kb_config_not_sync_config(monkeypatch, tmp_path):
    seen = {}
    for name in _CORE:
        monkeypatch.setattr(cli, name, lambda *a, **k: None)
    for name in _KB:
        monkeypatch.setattr(kb, name, lambda a, _n=name: (seen.__setitem__(_n, a), 0)[1])
    cli._bootstrap(_args(no_sync=True, config="/sync.ini", kb_config="/kb.toml"),
                   {}, str(tmp_path), "grp")
    # kb stages get the kb.toml + the workspace, never the sync INI
    assert seen["cmd_index"].config == "/kb.toml"
    assert seen["cmd_index"].workspace == str(tmp_path)
    assert seen["cmd_steer"].out == str(tmp_path)
