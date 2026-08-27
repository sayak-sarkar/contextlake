"""`--config` must never send the knowledge stages to a store the user did not name.

Two doors reached the same outcome, and BOTH were exercised by accident during an audit:
six repository rows and four nodes were written into a production store by commands that
each carried an explicit `--config`.

* **Door 1.** On `bootstrap`, `--config` is the *mirror INI*; the knowledge stages take
  `--kb-config`. Passing a `kb.toml` to `--config` parsed TOML as INI (silently yielding
  the default work_dir and group) and left the kb stages on `~/.contextlake/kb.toml`.
* **Door 2.** A `--config` that exists but sets no `[kb] store_dir` is *merged over* the
  global config, so it inherits the global store. `load_kb_config` already hard-errors
  when `--config` does not exist, for exactly this reason -- "which can point at a
  completely different (possibly production) store" -- and this is the same hazard by a
  quieter route.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run(args, cwd, home):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1"}
    return subprocess.run([sys.executable, "-m", "contextlake", *args], cwd=str(cwd),
                          env=env, capture_output=True, text=True)


def test_bootstrap_refuses_a_kb_toml_passed_to_the_mirror_config_flag(tmp_path):
    """THE LOAD-BEARING ASSERTION: it must refuse, name the right flag, and EXIT NON-ZERO.

    The exit code is half the test. `_bootstrap`'s return value was discarded at the
    dispatch, so a refusal printed its message and still exited 0 -- reporting failure
    while claiming success, which is the defect class this release exists to close.
    """
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    kb_toml = tmp_path / "kb.toml"
    kb_toml.write_text(f'[kb]\nstore_dir = "{(tmp_path / "store").as_posix()}"\n',
                       encoding="utf-8")

    r = _run(["bootstrap", "--config", str(kb_toml), "--group", "demo-org",
              "--workspace", str(ws), "--no-sync"], cwd=tmp_path, home=home)
    out = r.stdout + r.stderr
    assert r.returncode == 2, (
        f"a refused bootstrap exited {r.returncode}; it must be non-zero or scripts and "
        f"CI read the refusal as a success.\n{out[-2000:]}")
    assert "--kb-config" in out, "the message must name the flag that would have worked"
    assert "mirror INI" in out


# --- B0-5 / B0-6: a partial success is not a failure, and a supplied --group is a group --

def test_the_no_config_warning_does_not_fire_when_group_was_supplied(tmp_path):
    """`--group` is merged into the effective config AFTER `load_config` runs, so the
    "no config with your group was found" warning fired on every single `--group`
    invocation of every mirror command -- telling a user who had just supplied the group
    that no group was found.

    A warning that is wrong on a correct invocation is worse than no warning: it trains
    people to ignore the channel that also carries the real ones.
    """
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)

    with_group = _run(["mirror", "status", "--group", "demo-org"], cwd=tmp_path, home=home)
    assert "still the placeholder" not in (with_group.stdout + with_group.stderr), (
        "the no-config warning fired even though --group was supplied")


def test_the_no_config_warning_still_fires_when_there_is_genuinely_no_group(tmp_path):
    """The near-miss. Suppressing the false alarm must not suppress the true one --
    otherwise a user with no configuration at all gets no guidance."""
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)

    without = _run(["mirror", "status"], cwd=tmp_path, home=home)
    assert "still the placeholder" in (without.stdout + without.stderr), (
        "a genuinely unconfigured run was left with no warning at all")


def test_store_has_repos_answers_true_when_it_cannot_tell(tmp_path):
    """`_store_has_repos` gates whether bootstrap aborts after a partial index. It must
    fail OPEN: refusing to run the remaining stages because the check itself broke is a
    worse outcome than the hollow success it guards against."""
    from types import SimpleNamespace

    from contextlake.cli import _store_has_repos

    # A namespace that cannot resolve to any store at all.
    assert _store_has_repos(SimpleNamespace(config="/nonexistent/nope.toml")) is True


def test_the_config_guard_runs_before_anything_else_in_bootstrap():
    """Position matters, and CI proved it.

    The guard was originally placed after the mirror, after the audit, and after
    `from .kb import commands` -- whose ImportError path `return`s. So on a core-only
    install (no `[kb]` extra) the refusal never ran and `bootstrap --config <kb.toml>`
    exited 0. Every `core` job in the matrix failed on exactly that while every
    `knowledge-layer` job passed, which is the signature of a core-tier-only defect.

    Asserted by source position rather than by behaviour, because reproducing it needs
    an interpreter without the kb extra -- which CI has and a developer machine does
    not. A behavioural test here would pass locally forever.
    """
    import inspect

    from contextlake import cli

    src = inspect.getsource(cli._bootstrap)
    # Anchored on the STATEMENTS, not on prose. The first version searched for
    # "from .kb import commands" and matched this fix's own explanatory comment, which
    # sits above the guard -- so the test failed against correct code. A source-position
    # assertion is only as good as the uniqueness of what it looks for.
    i_guard = src.find('if getattr(args, "config", None) and not getattr(args, "kb_config", None):')
    i_kb_import = src.find("        from .kb import commands as kb")
    i_mirror = src.find("Skipping the GitLab mirror step (--no-sync)")

    assert i_guard > 0, "the --config/--kb-config guard is gone"
    assert i_kb_import > 0, "the kb import moved; re-check this test's anchors"
    assert i_guard < i_kb_import, (
        "the guard sits below `from .kb import commands`, whose ImportError path "
        "returns -- so on a core-only install the refusal is skipped and bootstrap "
        "exits 0. This is the exact regression CI caught.")
    assert i_guard < i_mirror, (
        "argument validation runs after the mirror, so a user pays a full mirror pass "
        "before being told their flags are wrong")


def test_bootstrap_force_reaches_the_index_stage():
    """copy.copy(args) is what carries it, so the flag existing on the parser
    is the whole wiring. Asserted because a flag that parses and is then
    ignored is indistinguishable from a working one."""
    from contextlake.cli import build_parser

    assert build_parser().parse_args(["bootstrap", "--force"]).force is True
    assert build_parser().parse_args(["bootstrap"]).force is False


def test_bootstrap_force_does_not_reach_the_steer_stage(monkeypatch, tmp_path):
    """`args.force` means two different things on two different stages, both
    read via the same `kb_args` object `_bootstrap` builds with one
    `copy.copy(args)`: on `index`/`embed`/`wiki` it means "rebuild", on `steer`
    it means "overwrite non-managed files" (a user's hand-edited AGENTS.md).

    `schedule`'s periodic full cycle runs `bootstrap --force` on a repeating
    interval, so without the reset in the stage loop this would overwrite an
    edited AGENTS.md every `schedule_full_every`, which nothing about
    `bootstrap --force`'s own help text says it does.
    """
    import pytest

    from contextlake import cli

    kb = pytest.importorskip("contextlake.kb.commands")

    seen = {}

    def _record(name):
        def _fn(args):
            seen[name] = getattr(args, "force", None)
            return 0
        return _fn

    monkeypatch.setattr(kb, "cmd_index", _record("index"))
    monkeypatch.setattr(kb, "cmd_steer", _record("steer"))

    args = cli.build_parser().parse_args([
        "bootstrap", "--force", "--no-sync", "--no-connect", "--no-embed",
        "--no-enrich", "--no-wiki", "--no-diagrams", "--no-docs",
        "--workspace", str(tmp_path)])

    # Both stubbed stages return 0, so this is the plain-success path: no
    # failures means _bootstrap logs a summary and returns, it does not exit.
    cli._bootstrap(args, {"work_dir": str(tmp_path), "gitlab_group": "g"},
                   str(tmp_path), "g")

    assert seen["index"] is True, "index must still see --force"
    assert seen["steer"] is False, "steer must NOT inherit index's --force"


# --- bootstrap --workers: the same shape as --force above, and the same gap ---
# `bootstrap` is jobs.DEFAULT_ARGV, the default scheduled job, so a fleet run on a
# schedule with no --workers cap still ran the index stage at the unbounded auto
# default. Checked whether any other stage reads args.workers for a different
# meaning the way cmd_steer reads args.force: connect, embed, enrich, wiki, docs
# and steer none of them reference "workers" at all, so -- unlike --force -- no
# stage guard is needed here.

_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text("def foo():\n    return 1\n")
    _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c"], path)


def test_bootstrap_workers_flag_parses_and_reaches_args():
    """Same precedent as --force: the parser declaration is the whole plumbing,
    since copy.copy(args) carries every attribute into kb_args."""
    from contextlake.cli import build_parser

    assert build_parser().parse_args(["bootstrap", "--workers", "2"]).workers == 2
    assert build_parser().parse_args(["bootstrap"]).workers is None


def test_bootstrap_workers_reaches_the_index_stage(tmp_path, gls_logs):
    """The parser accepting a flag proves nothing about a stage honouring it --
    that is the whole reason this gap existed. Assert on the worker count
    _index_workspace actually logs when driven through a real _bootstrap run,
    not on the parsed namespace."""
    import pytest

    from contextlake import cli

    # This test imports nothing from contextlake.kb, so the static tier check in
    # test_core_tier_has_no_kb_imports cannot see it, but it depends on the
    # knowledge layer at RUNTIME: it drives a real bootstrap and asserts on a
    # line only the index stage logs. Without the [kb] extra that stage never
    # runs, the line never appears, and the assertion fails on all four core
    # cells. A static import check catches imports, not runtime dependence.
    pytest.importorskip("contextlake.kb.cmds.index")

    ws = tmp_path / "ws"
    _git_repo(ws / "one")
    _git_repo(ws / "two")
    store_dir = tmp_path / "kb"
    kb_cfg = tmp_path / "kb.toml"
    kb_cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')

    args = cli.build_parser().parse_args([
        "bootstrap", "--no-sync", "--no-audit", "--no-connect", "--no-embed",
        "--no-enrich", "--no-wiki", "--no-diagrams", "--no-docs",
        "--workspace", str(ws), "--kb-config", str(kb_cfg), "--workers", "1"])

    cli._bootstrap(args, {"work_dir": str(ws), "gitlab_group": "g"}, str(ws), "g")

    assert "with 1 worker(s)" in gls_logs.text, gls_logs.text


def test_bootstrap_omitting_workers_uses_the_auto_default(tmp_path, gls_logs):
    """No --workers on the command line must still resolve to the same auto
    default _index_workspace would pick on its own, not to some other value a
    wrong wiring could silently substitute."""
    import pytest

    from contextlake import cli

    # Guarded like the same import at line 163 and in test_schedule_activity_wiring:
    # this file runs in CI's core job, which installs no [kb] extra. Unguarded, the
    # static tier check in test_core_tier_has_no_kb_imports fails in BOTH jobs,
    # because it reads the file rather than importing it.
    index_mod = pytest.importorskip("contextlake.kb.cmds.index")
    _default_index_workers = index_mod._default_index_workers

    ws = tmp_path / "ws"
    _git_repo(ws / "one")
    store_dir = tmp_path / "kb"
    kb_cfg = tmp_path / "kb.toml"
    kb_cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')

    args = cli.build_parser().parse_args([
        "bootstrap", "--no-sync", "--no-audit", "--no-connect", "--no-embed",
        "--no-enrich", "--no-wiki", "--no-diagrams", "--no-docs",
        "--workspace", str(ws), "--kb-config", str(kb_cfg)])

    cli._bootstrap(args, {"work_dir": str(ws), "gitlab_group": "g"}, str(ws), "g")

    expected = _default_index_workers()
    assert f"with {expected} worker(s)" in gls_logs.text, gls_logs.text
