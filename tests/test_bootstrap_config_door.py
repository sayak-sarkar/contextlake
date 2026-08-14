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
