"""The first-five-minutes walls, from a recorded walkthrough of a brand-new install.

Each test here pins one place where the tool answered a beginner in a way that was
technically correct and practically misleading. They are grouped because they share a
cause: a surface that says the true thing about ITS OWN operation while leaving the reader
with a false impression of the system.

Every assertion is paired with its negative case in the same file. A note that fires on
every run is noise, and noise is how a warning stops being read.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SOURCE = """\
def hook(x):
    return x


class Evolver:
    def hook(self, y):
        return y


def test_hook_evolve_name(z):
    def hook(inner):
        return inner
    return hook(z)
"""


def _home(tmp_path, name):
    home = tmp_path / name / "home"
    (home / ".contextlake").mkdir(parents=True)
    store = tmp_path / name / "store"
    (home / ".contextlake" / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store}"\n[embeddings]\nenabled = false\n', encoding="utf-8")
    return home, store


def _run(args, home, cwd=None):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1"}
    return subprocess.run([sys.executable, "-m", "contextlake", *args],
                          cwd=str(cwd or home), env=env, capture_output=True, text=True)


def _indexed(tmp_path, name, repos=("demo",)):
    home, store = _home(tmp_path, name)
    for r in repos:
        d = tmp_path / name / "ws" / r
        (d / "pkg").mkdir(parents=True)
        (d / "pkg" / "a.py").write_text(SOURCE, encoding="utf-8")
        ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
        subprocess.run(["git", *ident, "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", *ident, "commit", "-qm", "i"], cwd=d, check=True)
        r = _run(["kb", "index", str(d)], home)
        assert r.returncode == 0, f"indexing failed:\n{(r.stdout + r.stderr)[-1500:]}"
    return home, store


# --- an empty store must not answer like a genuine miss ----------------------

def test_a_query_against_an_empty_store_says_so(tmp_path):
    """Querying also CREATES the store file, so "I forgot to index" and "indexed, no
    such symbol" printed the same line with a fresh empty database behind one of them."""
    home, store = _home(tmp_path, "empty")
    r = _run(["kb", "query", "Serializer"], home)
    out = r.stdout + r.stderr
    assert "No matches" in out
    assert "empty" in out.lower(), f"an empty store answered like a real miss:\n{out}"
    assert str(store) in out, (
        "the store path is the load-bearing half: the usual cause is the right command "
        f"against the wrong store, and the path is what shows it. Got:\n{out}")


def test_a_real_miss_on_a_populated_store_stays_quiet(tmp_path):
    """The negative case. Without this the test above passes on a note that fires
    always, which would train the reader to skip it."""
    home, _ = _indexed(tmp_path, "populated")
    out = (lambda r: r.stdout + r.stderr)(_run(["kb", "query", "Serializer"], home))
    assert "No matches" in out
    assert "empty" not in out.lower(), f"a populated store claimed to be empty:\n{out}"


# --- a hit must show the field that matched ----------------------------------

def test_text_hits_show_the_qualified_name_that_explains_them(tmp_path):
    """Three functions named `hook` came back and the printed lines were identical
    apart from the line number. The qualified name is what tells them apart, and
    `--json` had carried it all along."""
    home, _ = _indexed(tmp_path, "qual")
    out = (lambda r: r.stdout + r.stderr)(_run(["kb", "query", "hook"], home))
    assert "Evolver.hook" in out, f"the method's qualified name is missing:\n{out}"
    assert "test_hook_evolve_name.hook" in out, (
        f"the nested function's qualified name is missing:\n{out}")


# --- the fleet map is not the picture a one-repo user wants ------------------

def test_overview_on_a_single_repo_store_names_the_useful_command(tmp_path):
    home, _ = _indexed(tmp_path, "one")
    out = (lambda r: r.stdout + r.stderr)(_run(["kb", "graph", "--overview"], home))
    assert "FLEET map" in out, f"no note on a one-repo fleet map:\n{out}"
    assert "--repo demo" in out, (
        f"the suggested command must carry a real repo id, not an empty one:\n{out}")


def test_overview_on_a_multi_repo_store_stays_quiet(tmp_path):
    home, _ = _indexed(tmp_path, "many", repos=("alpha", "beta"))
    out = (lambda r: r.stdout + r.stderr)(_run(["kb", "graph", "--overview"], home))
    assert "FLEET map" not in out, (
        f"the one-repo note fired on a store with two repositories:\n{out}")


# --- do not tell someone to install what they are running --------------------

def test_init_does_not_advise_installing_an_extra_that_is_present(tmp_path):
    """`init` closed with `pip install "contextlake[kb]"` even when the kb extra was
    already importable in the running interpreter, which reads as "your install was
    wrong". This test env has the extra, so the line must be absent."""
    home, _ = _home(tmp_path, "init")
    ws = tmp_path / "init" / "ws"
    ws.mkdir(parents=True)
    r = _run(["init", "--local", "--no-mirror", "--skip-interactive"], home, cwd=ws)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"init failed:\n{out[-1500:]}"
    assert "kb index" in out, f"init stopped naming the next command:\n{out}"
    assert 'pip install "contextlake[' not in out, (
        f"init told a user with the knowledge layer installed to install it:\n{out}")
