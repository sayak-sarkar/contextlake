"""`bootstrap` must write an API reference.

The API reference is one of the outputs this product exists to generate, and it is the cheapest
of them: no model, no network, one pass over shards already on disk. Leaving it out of the
single command documented as "from nothing to a wired workspace" would mean the output nobody
has to configure is the one nobody gets by default. The diagram stage was added for the same
reason, after being fully built and called by nothing.

Two assertions carry this file, and the second is the one worth having. Announcing the stage
proves the list was edited; reading the written page proves the stage did something, and the
quoted call site proves it read the working tree rather than only the graph. A test that
checked the banner alone would pass against a stage that wrote an empty file.

Lives under ``tests/kb/`` for the same reason as the diagram test: the core CI job runs
``pytest --ignore=tests/kb`` against an install carrying no kb extra, where `bootstrap` has no
knowledge-layer stage to run at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Both offline switches: `bootstrap` reaches the knowledge-layer stages without a forge group
# only when the mirror AND audit steps are off (see cli._needs_group).
OFFLINE = ["bootstrap", "--no-sync", "--no-audit", "--no-connect", "--no-embed",
           "--no-enrich", "--no-wiki", "--no-diagrams"]

# `run` calls `helper` on a line of its own, so the quoted source can be asserted exactly. If
# the call shared a line with anything else, "the right line" and "a line" would be
# indistinguishable.
SOURCE = (
    "def helper(x):\n"
    "    return x + 1\n"
    "\n"
    "\n"
    "def run():\n"
    "    return helper(1)\n"
)
THE_CALL = "return helper(1)"


def _repo(path: Path) -> None:
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "a.py").write_text(SOURCE, encoding="utf-8")
    run = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    subprocess.run([*run, "add", "-A"], cwd=path, check=True)
    subprocess.run([*run, "commit", "-qm", "init"], cwd=path, check=True)


def _bootstrap(tmp_path: Path, name: str, extra=()):
    """Run a real offline bootstrap over one repo and return (result, docs/api dir)."""
    home = tmp_path / name / "home"
    (home / ".contextlake").mkdir(parents=True)
    store = tmp_path / name / "store"
    ws = tmp_path / name / "ws"
    _repo(ws / "solo")
    (home / ".contextlake" / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store}"\n[embeddings]\nenabled = false\n', encoding="utf-8")

    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1"}
    r = subprocess.run([sys.executable, "-m", "contextlake", *OFFLINE, *extra,
                        "--workspace", str(ws)],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True)
    return r, store / "docs" / "api"


def test_bootstrap_writes_a_reference_carrying_a_real_call_site(tmp_path):
    r, api = _bootstrap(tmp_path, "on")
    assert r.returncode == 0, f"bootstrap failed:\n{(r.stdout + r.stderr)[-2000:]}"
    assert "Write the API reference" in r.stdout + r.stderr, (
        "bootstrap ran without an API-reference stage")

    pages = sorted(api.glob("*.md")) if api.is_dir() else []
    assert pages, (
        f"the stage announced itself and wrote nothing; store holds "
        f"{sorted(p.name for p in api.glob('*')) if api.is_dir() else 'no docs/api dir'}")
    body = pages[0].read_text(encoding="utf-8")
    assert "API reference" in body
    # The graph half: `helper` is called once, from `run`.
    assert "`run` *(function)*" in body
    # The working-tree half: the line at that call site was read off disk and quoted. This is
    # what separates a reference from a list of pointers, and it is only reachable when the
    # freshness gate passes, so it also proves a just-indexed tree is quotable.
    assert f"`{THE_CALL}`" in body, (
        f"no quoted call-site source in a freshly indexed tree:\n{body[:1200]}")


def test_no_docs_skips_the_stage(tmp_path):
    r, api = _bootstrap(tmp_path, "off", extra=["--no-docs"])
    assert r.returncode == 0, f"bootstrap failed:\n{(r.stdout + r.stderr)[-2000:]}"
    assert "Write the API reference" not in r.stdout + r.stderr, (
        "--no-docs did not skip the stage")
    assert not api.exists() or not any(api.glob("*.md")), (
        "--no-docs still wrote a reference")
