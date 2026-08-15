"""`bootstrap` must produce an architecture diagram.

Architecture drawings are one of the outputs this product exists to generate, and
`bootstrap` -- the single command documented as "from nothing to a wired workspace" --
ran index, connect, embed, enrich, wiki and steer, and never drew one. The capability
was fully built (`kb graph`); nothing called it.

The second assertion here is the one that makes the stage worth having. `--overview`
renders the FLEET map, with repositories as nodes, so on a store holding a single
repository it is correct and useless: one node and no edges. A store's shape decides
which view the stage draws, and this file pins that choice by measuring both shapes in
one run -- a test that only ever saw one workspace could not tell a working chooser
from a hardcoded answer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Both offline switches: `bootstrap` reaches the knowledge-layer stages without a forge
# group only when the mirror AND audit steps are off (see cli._needs_group).
OFFLINE = ["bootstrap", "--no-sync", "--no-audit", "--no-connect", "--no-embed",
           "--no-enrich", "--no-wiki"]

SOURCE = (
    "def helper(x):\n"
    "    return x + 1\n"
    "\n"
    "\n"
    "class Widget:\n"
    "    def run(self):\n"
    "        return helper(1)\n"
)


def _repo(path: Path) -> None:
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "a.py").write_text(SOURCE, encoding="utf-8")
    run = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    subprocess.run([*run, "add", "-A"], cwd=path, check=True)
    subprocess.run([*run, "commit", "-qm", "init"], cwd=path, check=True)


def _bootstrap(tmp_path: Path, name: str, repos: list[str], extra=()):
    """Run a real offline bootstrap over `repos` and return (result, graphs_dir)."""
    home = tmp_path / name / "home"
    (home / ".contextlake").mkdir(parents=True)
    store = tmp_path / name / "store"
    ws = tmp_path / name / "ws"
    for r in repos:
        _repo(ws / r)
    # Embeddings off: this measures the diagram stage, and a model download would make
    # the test's outcome depend on the network.
    (home / ".contextlake" / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store}"\n[embeddings]\nenabled = false\n', encoding="utf-8")

    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1"}
    r = subprocess.run([sys.executable, "-m", "contextlake", *OFFLINE, *extra,
                        "--workspace", str(ws)],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True)
    return r, store / "graphs"


def test_bootstrap_draws_a_diagram_and_picks_the_view_from_the_store_shape(tmp_path):
    one, one_graphs = _bootstrap(tmp_path, "one", ["solo"])
    many, many_graphs = _bootstrap(tmp_path, "many", ["alpha", "beta"])

    for r in (one, many):
        assert r.returncode == 0, f"bootstrap failed:\n{(r.stdout + r.stderr)[-2000:]}"
        assert "Draw the architecture" in r.stdout + r.stderr, (
            "bootstrap ran without a diagram stage")

    # A single-repo store gets the symbol graph. `overview.html` here would be the
    # one-node fleet map -- technically written, and worth nothing to the reader.
    assert (one_graphs / "graph.html").is_file(), (
        f"no repo-view diagram for a single-repo store; graphs dir holds "
        f"{sorted(p.name for p in one_graphs.glob('*')) if one_graphs.is_dir() else 'nothing'}")
    assert not (one_graphs / "overview.html").exists(), (
        "a single-repo store got the fleet map, which is one node and no edges")

    # The other shape, in the same test, so the assertion above cannot pass merely
    # because the stage always writes graph.html.
    assert (many_graphs / "overview.html").is_file(), (
        "a multi-repo store did not get the fleet map")


def test_no_diagrams_skips_the_stage(tmp_path):
    r, graphs = _bootstrap(tmp_path, "off", ["solo"], extra=["--no-diagrams"])
    assert r.returncode == 0, f"bootstrap failed:\n{(r.stdout + r.stderr)[-2000:]}"
    assert "Draw the architecture" not in r.stdout + r.stderr, (
        "--no-diagrams did not skip the stage")
    assert not graphs.exists() or not any(graphs.glob("*.html")), (
        "--no-diagrams still wrote a diagram")
