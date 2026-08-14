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
