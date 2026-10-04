"""`--dry-run` must never be accepted by a command that would ignore it (W05).

The root and both namespace parsers take `--dry-run` ahead of the verb, because the mirror
verbs need that spelling. So `contextlake --dry-run kb index` parsed cleanly and ran for
real: `kb index`, `kb wiki`, `kb embed` and the rest never read the flag. `bootstrap
--dry-run` was the worst case, because the flag reached only its mirror stage and every
knowledge-layer stage then ran for real (writing the store, and the wiki stage may call a
paid model).

The rules pinned here:

* a command that does not read the flag refuses it with exit 2 and runs nothing, in any
  position;
* a command that does read it keeps working;
* `bootstrap --dry-run` prints the stages it would run and does none of them, and records
  no metrics or run history for the run it did not do.

Safety of the tests themselves. They must be safe with the guard removed, because that is
how a break-test runs them. So no test here lets a real command, a real model or the
network run: the dispatch tables, the mirror stages, `init`, `completion` and `schedule`
are all replaced by recorders, the model and embedder factories raise, and the end-to-end
cases at the bottom run in a subprocess whose HOME is a temporary directory.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import contextlake.kb.commands as kb_commands
from contextlake import cli, core, init_cmd, netguard
from contextlake.cli import build_parser
from contextlake.config import DEFAULT_CONFIG
from contextlake.kb.config import load_kb_config
from contextlake.schedule import cmds as schedule_cmds

REPO = Path(__file__).resolve().parents[2]

# One valid argument tail for every command that does NOT read --dry-run. The coverage
# test below fails when a command is added or loses the flag without this table following.
TAILS = {
    "completion": [], "connect": [], "dashboard": [], "docs": [], "embed": [],
    "enrich": [], "eval": [], "graph": [], "hook": ["status"], "impact": ["x"],
    "index": ["x"], "ingest": [], "init": [], "keys": ["list"], "lint": [],
    "owners": ["x"], "query": ["x"], "refresh": [], "serve": [],
    "source": ["list"], "steer": [], "version": [], "wiki": [],
}
# Commands whose own parser declares --dry-run, so they read it. `schedule` reads it on
# `install` only and refuses it on its other actions itself (cmds.FLAG_ACTIONS; see
# tests/test_schedule_flag_scope.py).
TAKERS = ["audit", "bootstrap", "branches", "clone", "doctor", "fetch", "forget", "schedule",
          "status", "sync", "update", "verify"]

_MIRROR_ENTRY_STAGE = {
    "fetch": "fetch_gitlab_projects", "clone": "clone_missing_repos",
    "update": "update_repositories", "branches": "switch_repository_branches",
    "verify": "verify_structure", "sync": "fetch_gitlab_projects",
    "status": "show_status", "audit": "run_audit",
}


def _argv(command, *, position, tail):
    """`command` with --dry-run (or -n) in one of the three pre-verb positions."""
    ns = cli._NAMESPACE_OF.get(command)
    flag = "-n" if position == "root-n" else "--dry-run"
    if position == "namespace":
        return ([ns, flag, command, *tail] if ns else [flag, command, *tail])
    return ([flag, ns, command, *tail] if ns else [flag, command, *tail])


def _snapshot(root: Path):
    """Every path under `root` with its size and mtime: catches a created directory or
    a rewritten file, not only a new file."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out[str(p.relative_to(root))] = (p.is_dir(), st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A machine with nothing on it, and every way a command could do real work replaced
    by a recorder. `calls` lists everything that ran, by name."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    monkeypatch.delenv("CONTEXTLAKE_SCHEDULE_HISTORY", raising=False)
    # `config.CONFIG_FILE` was resolved from the REAL home when the module was imported,
    # so redirecting HOME alone leaves it pointing at the operator's own file.
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    monkeypatch.setattr("contextlake.config.LOCAL_CONFIG_FILE", str(tmp_path / "no-local.ini"))
    # `DEFAULT_CONFIG["work_dir"]` was also resolved from the real home at import time.
    monkeypatch.setitem(DEFAULT_CONFIG, "work_dir", str(tmp_path / "work"))
    monkeypatch.chdir(cwd)

    # Canaries: if any fails, a path this test could write is not under tmp_path.
    assert os.path.expanduser("~") == str(home)
    assert tmp_path in load_kb_config(None).store_path.parents
    assert tmp_path in Path(DEFAULT_CONFIG["work_dir"]).parents

    calls: list[str] = []

    def recorder(name, ret=None):
        def fn(*a, **k):
            calls.append(name)
            return ret
        return fn

    def boom(name):
        def fn(*a, **k):
            calls.append(name)
            raise AssertionError(f"{name} was constructed by a dry run")
        return fn

    monkeypatch.setattr(kb_commands, "dispatch", recorder("kb.dispatch", 0))
    for n in [n for n in dir(kb_commands) if n.startswith("cmd_")]:
        monkeypatch.setattr(kb_commands, n, recorder(f"kb.{n}", 0))
    monkeypatch.setattr(init_cmd, "cmd_init", recorder("cmd_init", 0))
    monkeypatch.setattr(init_cmd, "cmd_completion", recorder("cmd_completion", 0))
    monkeypatch.setattr(init_cmd, "maybe_auto_register_completion",
                        recorder("auto_register_completion"))
    monkeypatch.setattr(schedule_cmds, "dispatch", recorder("schedule.dispatch", 0))
    for mod, name in (("contextlake.kb.llm", "build_llm"),
                      ("contextlake.kb.llm", "build_review_llm"),
                      ("contextlake.kb.llm.base", "build_llm"),
                      ("contextlake.kb.llm.base", "build_review_llm"),
                      ("contextlake.kb.embeddings", "build_embedder"),
                      ("contextlake.kb.embeddings.base", "build_embedder")):
        monkeypatch.setattr(f"{mod}.{name}", boom(name))

    seen_config: dict[str, dict] = {}

    def mirror_stage(name, ret):
        def fn(*a, **k):
            calls.append(name)
            seen_config[name] = next((x for x in a if isinstance(x, dict) and "work_dir" in x),
                                     None)
            return ret
        return fn

    monkeypatch.setattr(cli, "fetch_gitlab_projects",
                        mirror_stage("fetch_gitlab_projects", {"grp/a": {}}))
    for n in ("clone_missing_repos", "update_repositories", "switch_repository_branches",
              "verify_structure"):
        monkeypatch.setattr(cli, n, mirror_stage(n, core.StageResult()))
    monkeypatch.setattr(cli, "show_status", mirror_stage("show_status", None))
    monkeypatch.setattr(cli, "run_audit", mirror_stage("run_audit", None))
    # --offline installs a one-way, process-wide socket guard. Never in the test process.
    monkeypatch.setattr(netguard, "install", lambda: None)

    class World:
        pass

    w = World()
    w.tmp, w.home, w.cwd, w.calls, w.config = tmp_path, home, cwd, calls, seen_config
    w.patch = monkeypatch
    w.snapshot = lambda: (_snapshot(home), _snapshot(cwd))
    return w


def _run(argv):
    """cli.main(argv) -> exit code (0 for a normal return)."""
    try:
        cli.main(argv)
    except SystemExit as e:
        return 0 if e.code is None else e.code
    return 0


# --- the refusal ---------------------------------------------------------------------

def test_the_table_covers_every_command_that_ignores_the_flag():
    """Derived from the parsers, so a new command cannot dodge the matrix below."""
    flags = build_parser()._flags_by_command()
    ignoring = {c for c, f in flags.items() if "--dry-run" not in f}
    taking = {c for c, f in flags.items() if "--dry-run" in f}
    assert ignoring == set(TAILS), (sorted(ignoring ^ set(TAILS)))
    assert taking == set(TAKERS), (sorted(taking ^ set(TAKERS)))


# A top-level command has no namespace position, so it gets two spellings, not three.
_REFUSAL_CASES = [(c, p) for c in sorted(TAILS) for p in ("root", "root-n", "namespace")
                  if p != "namespace" or c in cli._NAMESPACE_OF]


@pytest.mark.parametrize("command,position", _REFUSAL_CASES)
def test_a_command_that_ignores_dry_run_refuses_it_and_runs_nothing(
        world, capsys, command, position):
    before = world.snapshot()

    code = _run(_argv(command, position=position, tail=TAILS[command]))

    err = capsys.readouterr().err
    assert code == 2, err
    assert world.calls == [], world.calls          # no stage, command, model or embedder
    assert world.snapshot() == before              # nothing written under HOME or cwd
    assert "isn't a flag on" in err and command in err
    # It names every command that DOES take the flag, so the user knows where to go.
    for taker in TAKERS:
        assert cli._qualified(taker) in err


def test_the_refusal_names_the_qualified_command_and_the_ones_that_work(world, capsys):
    assert _run(["--dry-run", "kb", "index", "x"]) == 2
    err = capsys.readouterr().err
    assert "'--dry-run' isn't a flag on 'kb index'" in err
    assert "It's used by: " in err
    assert "bootstrap, doctor, kb forget, mirror audit" in err


def test_an_alias_is_refused_under_its_canonical_name(world, capsys):
    assert _run(["--dry-run", "kb", "who-knows", "x"]) == 2
    assert "'kb owners'" in capsys.readouterr().err
    assert world.calls == []


def test_after_the_verb_the_flag_is_still_refused_too(world, capsys):
    """The one position that was already refused, kept as a regression guard."""
    assert _run(["kb", "index", "x", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "isn't a flag on" in err and "kb forget" in err
    assert world.calls == []


def test_the_flag_is_not_refused_when_it_is_not_given(world):
    """Positive control: the same commands run when --dry-run is absent, so the refusal
    tests above are not passing because the stubs make everything fail."""
    assert _run(["kb", "index", "x"]) == 0
    # Completion registration runs on a normal invocation: the control for the test below.
    assert world.calls == ["auto_register_completion", "kb.dispatch"]
    world.calls.clear()
    assert _run(["init"]) == 0
    assert world.calls == ["cmd_init"]
    world.calls.clear()
    assert _run(["schedule", "status"]) == 0
    assert world.calls == ["auto_register_completion", "schedule.dispatch"]


def test_a_refused_dry_run_does_not_register_shell_completion(world, capsys):
    """Registering completion edits a shell dotfile, which a refused or previewed run
    must not do. Asserted on the real `index` verb, which otherwise reaches it."""
    assert _run(["--dry-run", "kb", "index", "x"]) == 2
    assert "auto_register_completion" not in world.calls


# --- commands that read the flag keep working ------------------------------------------

@pytest.mark.parametrize("position", ["root", "root-n", "namespace"])
def test_kb_forget_still_receives_the_flag_from_every_position(world, position):
    seen = {}
    world.patch.setattr(kb_commands, "dispatch",
                        lambda command, args: seen.update(command=command, args=args) or 0)
    assert _run(_argv("forget", position=position, tail=["some/repo"])) == 0
    assert seen["command"] == "forget"
    assert seen["args"].dry_run is True


def test_doctor_still_receives_the_flag(world):
    seen = {}
    world.patch.setattr(kb_commands, "dispatch",
                        lambda command, args: seen.update(args=args) or 0)
    assert _run(["--dry-run", "doctor", "--fix"]) == 0
    assert seen["args"].dry_run is True


@pytest.mark.parametrize("position", ["root", "root-n", "namespace"])
@pytest.mark.parametrize("verb", sorted(_MIRROR_ENTRY_STAGE))
def test_every_mirror_verb_still_takes_dry_run(world, verb, position):
    argv = _argv(verb, position=position, tail=["--group", "demo-org"])
    assert _run(argv) == 0
    stage = _MIRROR_ENTRY_STAGE[verb]
    assert stage in world.calls
    assert world.config[stage]["dry_run"] == "true"


# --- bootstrap --dry-run: a plan, and nothing else -------------------------------------

_PLAN_ORDER = [
    "Mirror repositories from", "Audit repositories", "Index the code graph",
    "Connect knowledge sources", "Build semantic vectors", "Enrich from connected sources",
    "Generate the curated wiki", "Draw the architecture", "Write the API reference",
    "Write editor steering",
]


_PLAN_LINE = re.compile(r"^(?:\[[^\]]*\]\s+)?\s*\d+\. (.+)$")


def _plan_lines(out):
    """The numbered plan lines of bootstrap's output, without the log prefix."""
    return [m.group(1) for ln in out.splitlines() if (m := _PLAN_LINE.match(ln))]


def test_bootstrap_dry_run_prints_the_plan_and_runs_no_stage(world, capsys):
    before = world.snapshot()

    code = _run(["bootstrap", "--dry-run", "--group", "demo-org"])

    out = capsys.readouterr().out
    assert code == 0, out
    # Nothing ran: no mirror stage, no audit, no knowledge stage, no model, no embedder.
    assert world.calls == [], world.calls
    # Nothing written: not the store, not the cache directory, not the run history.
    assert world.snapshot() == before
    assert not (world.home / ".cache").exists()
    assert not (world.home / ".contextlake").exists()
    # The plan lists every stage a real run performs, in the order it performs them.
    steps = _plan_lines(out)
    assert len(steps) == len(_PLAN_ORDER), out
    for step, expected in zip(steps, _PLAN_ORDER, strict=True):
        assert expected in step, (step, expected)
    assert "DRY RUN: bootstrap ran none of these stages." in out
    # The workspace it names is under tmp_path, so nothing here pointed at the real home.
    assert f"Workspace: {world.tmp}" in out


def test_the_plan_is_the_list_the_real_run_executes(world, capsys):
    """Positive control, and the drift guard. Without --dry-run the same command runs
    the stages the plan named, in the plan's order."""
    assert _run(["bootstrap", "--group", "demo-org"]) == 0
    assert f"Working directory: {world.tmp}" in capsys.readouterr().out
    assert world.calls == [
        "auto_register_completion",
        "fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
        "switch_repository_branches", "verify_structure", "run_audit",
        "kb.cmd_index", "kb.cmd_connect", "kb.cmd_embed", "kb.cmd_enrich", "kb.cmd_wiki",
        "kb.cmd_graph", "kb.cmd_docs", "kb.cmd_steer",
    ]


def test_a_bootstrap_dry_run_records_no_metrics_and_no_history(world, capsys, monkeypatch):
    metrics = world.tmp / "run.prom"
    history = world.tmp / "history.jsonl"
    monkeypatch.setenv("CONTEXTLAKE_SCHEDULE_HISTORY", str(history))
    argv = ["bootstrap", "--group", "demo-org", "--metrics-file", str(metrics)]

    assert _run([*argv, "--dry-run"]) == 0
    assert not metrics.exists()    # a metrics file would stamp a no-op as a finished run
    assert not history.exists()    # a history row would drag the measured duration to ~0

    # Positive control: the same invocation without --dry-run does write both, so the
    # two absences above mean the dry run skipped them.
    assert _run(argv) == 0
    assert metrics.exists()
    assert history.exists()


def test_the_plan_reflects_the_skip_flags(world, capsys):
    assert _run(["bootstrap", "--dry-run", "--no-sync", "--no-audit", "--no-embed",
                 "--no-wiki"]) == 0
    out = capsys.readouterr().out
    steps = " | ".join(_plan_lines(out))
    for gone in ("Mirror repositories", "Audit repositories", "Build semantic vectors",
                 "Generate the curated wiki"):
        assert gone not in steps
    for kept in ("Index the code graph", "Connect knowledge sources",
                 "Enrich from connected sources", "Draw the architecture",
                 "Write the API reference", "Write editor steering"):
        assert kept in steps
    assert "Skipped by flags: --no-sync, --no-audit, --no-embed, --no-wiki." in out
    assert world.calls == []


def test_the_plan_reflects_offline_mode(world, capsys):
    assert _run(["bootstrap", "--dry-run", "--offline", "--group", "demo-org"]) == 0
    out = capsys.readouterr().out
    assert "Mirror repositories" not in " | ".join(_plan_lines(out))
    assert "The mirror would be skipped: offline mode." in out
    assert "Audit repositories" in out                     # offline skips only the mirror
    assert world.calls == []
    assert netguard._installed is False                    # the test process stays unguarded


def test_the_plan_says_when_the_knowledge_layer_is_not_installed(world, capsys, monkeypatch):
    import contextlake.kb as kb_pkg

    monkeypatch.delattr(kb_pkg, "commands")
    monkeypatch.setitem(sys.modules, "contextlake.kb.commands", None)
    assert _run(["bootstrap", "--dry-run", "--group", "demo-org"]) == 0
    out = capsys.readouterr().out
    assert "The knowledge-layer stages would be skipped" in out
    assert [s for s in _plan_lines(out) if "Index the code graph" in s] == []
    assert world.calls == []


def test_a_bootstrap_dry_run_refuses_what_a_real_run_refuses(world, capsys):
    """Placeholder group: same exit 2 as a real run, and no plan."""
    assert _run(["bootstrap", "--dry-run"]) == 2
    assert "Bootstrap plan" not in capsys.readouterr().out
    assert world.calls == []


def test_a_bootstrap_dry_run_refuses_a_kb_toml_passed_as_the_mirror_config(world, capsys):
    kb_toml = world.tmp / "kb.toml"
    kb_toml.write_text('[kb]\nstore_dir = "/nowhere"\n', encoding="utf-8")
    assert _run(["bootstrap", "--dry-run", "--group", "demo-org",
                 "--config", str(kb_toml)]) == 2
    out = capsys.readouterr().out
    assert "--kb-config" in out and "Bootstrap plan" not in out
    assert world.calls == []


def test_the_config_file_can_turn_dry_run_on_for_bootstrap_too(world, capsys):
    """`dry_run = true` in the mirror INI is the same request as the flag: a plan only."""
    ini = world.tmp / "mirror.ini"
    ini.write_text("[contextlake]\ngitlab_group = demo-org\ndry_run = true\n", encoding="utf-8")
    assert _run(["bootstrap", "--config", str(ini)]) == 0
    assert "Bootstrap plan (dry run)" in capsys.readouterr().out
    # Completion registration runs before any config is read, so only the flag spelling
    # can skip it; in a real session it fires at most once and says so.
    assert [c for c in world.calls if c != "auto_register_completion"] == []


def test_bootstrap_help_says_what_dry_run_does_there_and_mirror_help_does_not_change(capsys):
    """bootstrap's own help must not promise the mirror verbs' wording ("without cloning,
    updating, or switching branches"), because here it also means no index and no model."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bootstrap", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "print the stages this run would perform, and run none of them" in out
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mirror", "sync", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "without cloning, updating, or switching branches" in out


def test_bootstrap_stages_builds_the_list_without_running_anything(world):
    args = argparse.Namespace()
    stages = cli._bootstrap_stages(args, kb_commands)
    assert [t for t, _ in stages][0] == "Index the code graph"
    assert world.calls == []


# --- end to end, in a subprocess: the real CLI, a real store, a temporary HOME ----------

def _cli(args, *, home, cwd):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1",
           "CONTEXTLAKE_OFFLINE": "1", "CONTEXTLAKE_NO_AUTO_COMPLETION": "1"}
    return subprocess.run([sys.executable, "-m", "contextlake", *args], cwd=str(cwd),
                          env=env, capture_output=True, text=True, timeout=300)


@pytest.fixture
def machine(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("def hello():\n    return 1\n\n\ndef caller():\n"
                                "    return hello()\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return home, src, cwd


def test_e2e_a_real_index_does_write_the_store(machine):
    """Positive control for the two tests below: this harness DOES see a store appear."""
    home, src, cwd = machine
    r = _cli(["kb", "index", str(src)], home=home, cwd=cwd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / ".contextlake" / "kb" / "index.sqlite").exists()


@pytest.mark.parametrize("argv_for", [
    lambda src: ["--dry-run", "kb", "index", str(src)],
    lambda src: ["kb", "--dry-run", "index", str(src)],
    lambda src: ["-n", "kb", "index", str(src)],
])
def test_e2e_a_dry_run_kb_index_refuses_and_writes_no_store(machine, argv_for):
    home, src, cwd = machine
    before = (_snapshot(home), _snapshot(cwd), _snapshot(src))

    r = _cli(argv_for(src), home=home, cwd=cwd)

    assert r.returncode == 2, r.stdout + r.stderr
    assert "isn't a flag on 'kb index'" in r.stderr
    assert not (home / ".contextlake").exists()
    assert (_snapshot(home), _snapshot(cwd), _snapshot(src)) == before


def test_e2e_a_dry_run_bootstrap_writes_nothing_and_exits_zero(machine):
    home, src, cwd = machine
    before = (_snapshot(home), _snapshot(cwd), _snapshot(src))

    r = _cli(["bootstrap", "--dry-run", "--no-sync", "--no-audit", "--workspace", str(src)],
             home=home, cwd=cwd)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "Bootstrap plan (dry run)" in r.stdout
    assert "Index the code graph" in r.stdout
    assert (_snapshot(home), _snapshot(cwd), _snapshot(src)) == before
    assert not (home / ".contextlake").exists()      # no store
    assert not (home / ".cache").exists()            # no cache directory, no history row
