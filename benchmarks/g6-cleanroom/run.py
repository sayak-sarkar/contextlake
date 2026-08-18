#!/usr/bin/env python3
"""Can a clean machine install this from the index and do the whole thing?

Verified from the PUBLISHED ARTEFACT, never the working tree. The charter singles that out
because an editable install has masked a version mismatch twice: the tree and the released
build were different, and every check that read the tree agreed with itself.

One happy path is not a clean room, so this runs on the minimum supported interpreter and
the newest, and includes the shapes that have actually broken before: a second index over an
unchanged tree, an `--offline` run, and a repository with no manifest at all.

    python benchmarks/g6-cleanroom/run.py --version 7.30.0
    python benchmarks/g6-cleanroom/run.py --version 7.30.0 --pythons 3.10,3.13 --keep

Each interpreter gets its own HOME, so "no contextlake config" is a fact rather than an
assumption, and every invocation carries an explicit `--config` inside that home.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
sys.path.insert(0, str(HERE))
import checks  # noqa: E402 - deliberately after the path insert

#: A tiny public tree. Pinned, so the numbers are reproducible, and resolved from the remote
#: rather than written from memory: the sibling harness once carried a commit that did not
#: exist in its repository at all.
TREE_URL = "https://github.com/pallets/click"
TREE_REF = "8.4.2"
TREE_COMMIT = "b2e30a175449cfda909ee4fbf4a29a6a071cad53"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
        timeout: int = 1800):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          check=False, timeout=timeout)


class CleanRoom:
    """One interpreter, one empty HOME, one install straight from the index."""

    def __init__(self, python: str, version: str, root: Path):
        self.python = python
        self.version = version
        self.root = root
        self.home = root / "home"
        self.venv = root / "venv"
        self.tree = root / "tree"
        self.bare = root / "no-manifest"
        self.config = self.home / "kb.toml"
        self.store = self.home / "store"
        self.notes: list[str] = []

    # --- environment ---------------------------------------------------------

    def env(self, **extra) -> dict:
        """A process environment with nothing of this machine's contextlake in it."""
        e = dict(os.environ)
        e["HOME"] = str(self.home)
        e.pop("CONTEXTLAKE_CONFIG", None)
        e.update(extra)
        return e

    @property
    def cwd(self) -> Path:
        """Run from inside the room.

        Setting HOME alone does not isolate configuration: the loader walks from the process
        working directory up through every ancestor looking for `.contextlake.kb.toml` and
        deep-merges what it finds. With the work directory inside the operator's own
        workspace, a config in any parent could change embeddings, LLM or sources under a
        gate that reports a clean machine.
        """
        return self.home

    @property
    def cli(self) -> Path:
        return self.venv / "bin" / "contextlake"

    def build(self) -> bool:
        for p in (self.home, self.store):
            shutil.rmtree(p, ignore_errors=True)
        self.home.mkdir(parents=True)
        interpreter = shutil.which(f"python{self.python}")
        if interpreter is None:
            self.notes.append(f"python{self.python} is not on this machine")
            return False
        r = run([interpreter, "-m", "venv", str(self.venv)])
        if r.returncode != 0:
            self.notes.append(f"venv creation failed: {r.stderr.strip()[-200:]}")
            return False
        # From the INDEX, pinned to the released version. `--no-cache-dir` because a cached
        # wheel from an earlier build would defeat the whole point of reading the published
        # artefact.
        # `[kb-full]`, not `[kb]`. The gate asks for all six output types, and vector search
        # needs an embedder, which `[kb]` deliberately does not carry -- it is the extra for
        # the graph, full-text search, wiki and MCP server. Measured: a `[kb]` clean room
        # produces five of the six and reports the sixth honestly as unavailable, which is
        # the packaging working as documented rather than a defect.
        r = run([str(self.venv / "bin" / "pip"), "install", "--quiet", "--no-cache-dir",
                 f"contextlake[kb-full]=={self.version}"], timeout=3600)
        if r.returncode != 0:
            self.notes.append(f"install failed: {(r.stderr or r.stdout).strip()[-300:]}")
            return False
        return True

    def clone(self) -> bool:
        if self.tree.exists():
            shutil.rmtree(self.tree)
        r = run(["git", "clone", "--quiet", TREE_URL, str(self.tree)], timeout=900)
        if r.returncode != 0:
            self.notes.append(f"clone failed: {r.stderr.strip()[-200:]}")
            return False
        r = run(["git", "checkout", "--quiet", TREE_COMMIT], cwd=self.tree)
        if r.returncode != 0:
            self.notes.append(f"pinned commit not found: {r.stderr.strip()[-160:]}")
            return False
        self.bare.mkdir(parents=True, exist_ok=True)
        (self.bare / "solo.py").write_text(
            "def only_function():\n    return 1\n", encoding="utf-8")
        for cmd in (["git", "init", "-q", "."], ["git", "add", "-A"],
                    ["git", "-c", "user.email=g6@example.invalid", "-c", "user.name=g6",
                     "commit", "-qm", "no manifest"]):
            run(cmd, cwd=self.bare)
        return True

    def cl(self, *args: str, env: dict | None = None, timeout: int = 1800):
        return run([str(self.cli), "--config", str(self.config), *args],
                   cwd=self.cwd, env=env or self.env(), timeout=timeout)

    # --- the checks ----------------------------------------------------------

    def reported_version(self) -> str | None:
        r = run([str(self.cli), "--version"], env=self.env())
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def init(self) -> bool:
        # `--no-mirror`: this room indexes a tree already on disk, so there is no group to
        # mirror from. Without it `init` exits 2 and says so, naming this exact flag -- the
        # first version of this harness passed `--local` instead and treated the refusal as
        # a quirk to work around rather than as the instruction it was.
        r = run([str(self.cli), "--config", str(self.config), "init", "--no-mirror",
                 "--skip-interactive"], env=self.env())
        # `init` must SUCCEED. A first version wrote its own minimal config when it failed
        # and returned True anyway, so this gate could pass while the shipped `init` was
        # broken -- and `init` working on a clean machine is a large part of what the gate
        # exists to prove.
        if r.returncode != 0:
            self.notes.append(f"init exited {r.returncode}: "
                              f"{(r.stderr or r.stdout).strip()[-240:]}")
            return False
        if not self.config.exists():
            self.notes.append(f"init exited 0 but wrote no {self.config.name}")
            return False
        self.store = self._store_dir_from_config()
        return True

    def _store_dir_from_config(self) -> Path:
        """Where the PRODUCT decided to put the store, not where this harness guessed.

        `init` writes `store_dir = "~/.contextlake/kb"`, and `~` resolves against this
        room's HOME. A first version looked in a directory it had invented, found no wiki
        and no docs, and reported three outputs as "not produced" when all three were on
        disk under the path the product had chosen. Reading the config is the only way this
        can be right for whatever `init` writes next.
        """
        try:
            text = self.config.read_text(encoding="utf-8")
        except OSError:
            return self.home / "store"
        m = re.search(r'^\s*store_dir\s*=\s*"([^"]+)"', text, re.M)
        if not m:
            return self.home / "store"
        raw = m.group(1)
        # The PRODUCT's expansion, run with this room's HOME, rather than a reimplementation
        # of it. It applies `expandvars` as well as `expanduser`, so a `$HOME`-relative
        # store_dir would have resolved against the caller's home in a hand-rolled version
        # and sent every output probe to the wrong directory.
        real_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(self.home)
            return Path(os.path.expanduser(os.path.expandvars(raw)))
        finally:
            if real_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = real_home

    @staticmethod
    def _rebuilt_count(text: str) -> int | None:
        """How many repositories this run actually REBUILT.

        Read from what the command really writes: `Indexed <id>: N nodes, M edges` once per
        rebuilt repository, plus a `Workspace indexed: ...` summary. A first version matched
        a prefix the command does not emit and then fell back to any mention of "unchanged",
        which the FIRST run's own `0 unchanged` satisfied -- so both runs read as zero and
        the check reported "second rebuilt nothing" for a pair where nothing was measured.
        """
        rebuilt = re.findall(r"Indexed \S+: \d+ nodes?", text)
        if rebuilt:
            return len(rebuilt)
        if re.search(r"unchanged|Workspace indexed", text, re.I):
            return 0
        return None

    def index_twice(self) -> tuple[int | None, int | None]:
        first = self.cl("kb", "index", str(self.tree))
        if first.returncode != 0:
            self.notes.append(f"first index exited {first.returncode}: "
                              f"{(first.stderr or first.stdout).strip()[-200:]}")
            return None, None
        second = self.cl("kb", "index", str(self.tree))
        return (self._rebuilt_count(first.stdout + first.stderr),
                self._rebuilt_count(second.stdout + second.stderr))

    def six_outputs(self) -> dict[str, bool]:
        """Each output type asked for by the shipped command, then looked for on disk."""
        produced: dict[str, bool] = {}
        lint = self.cl("kb", "lint", "--json")
        try:
            produced["code graph"] = json.loads(lint.stdout).get("repos", 0) > 0
        except (json.JSONDecodeError, AttributeError):
            pass

        self.cl("kb", "wiki")
        produced["wiki"] = any(self.store.glob("wiki/*.md"))

        self.cl("kb", "docs")
        produced["generated docs"] = any(self.store.glob("docs/api/*.md"))
        produced["fleet view"] = (self.store / "docs" / "fleet" / "design.md").exists()

        out = self.root / "graph.html"
        self.cl("kb", "graph", "--overview", "--output", str(out))
        produced["diagrams"] = out.exists() and out.stat().st_size > 0

        self.cl("kb", "embed", timeout=2400)
        q = self.cl("kb", "query", "command line option parsing", "--retriever", "semantic",
                    "--json")
        # Non-empty JSON is NOT enough. `cmd_query` degrades to full-text when the embedder
        # or the vector store is unavailable, and says so on stderr -- so a first version
        # would have marked "vector search" produced on pure keyword results, which is the
        # one thing this output type is not.
        degraded = re.search(r"showing fts results instead|cannot answer",
                             (q.stderr or "") + (q.stdout or ""), re.I)
        try:
            hits = bool(json.loads(q.stdout))
        except (json.JSONDecodeError, AttributeError):
            hits = False
        produced["vector search"] = hits and not degraded
        if degraded:
            self.notes.append("semantic search degraded to full-text, so the vector output "
                              "was not produced by the retriever it names")
        return produced

    def offline(self) -> tuple[bool | None, int | None, str]:
        """`--offline` must REFUSE a command the CLI itself lists as network-bound.

        A first version ran `kb lint` under poisoned proxies and read a zero exit as proof.
        But `kb lint` reads the local store and local git heads, and its own docstring says
        offline: it has no network path, so nothing was being tested. `mirror fetch` is in
        the CLI's `_NETWORK_MIRROR_COMMANDS`, so the guard has something to stop.
        """
        command = "mirror fetch"
        # A GROUP has to be configured, or the command is refused by config validation
        # before the guard is ever consulted -- "No group configured", exit 2, and nothing
        # about offline. The first version of this check read that refusal as the guard
        # speaking; it was the command never reaching it.
        ini = self.home / "mirror.ini"
        ini.write_text("[contextlake]\ngitlab_group = example-group\n"
                       f"work_dir = {self.root / 'mirror'}\n", encoding="utf-8")
        r = run([str(self.cli), "--config", str(ini), "--offline", "mirror", "fetch"],
                cwd=self.cwd, env=self.env(), timeout=600)
        text = ((r.stdout or "") + (r.stderr or "")).lower()
        refused = r.returncode != 0 and "offline" in text
        return refused, r.returncode, command

    def no_manifest(self) -> tuple[int | None, bool | None]:
        """Its OWN symbol has to be in the graph.

        A first version asked `kb lint` whether the store held two or more repositories,
        which the previously-indexed tree had already made true -- so an empty row for this
        one would have passed. The probe queries for the single function this tree defines,
        scoped to this repository.
        """
        r = self.cl("kb", "index", str(self.bare))
        if r.returncode != 0:
            return r.returncode, None
        q = self.cl("kb", "query", "only_function", "--repo", self.bare.name, "--json")
        try:
            hits = json.loads(q.stdout)
        except (json.JSONDecodeError, AttributeError):
            return r.returncode, None
        return r.returncode, any(h.get("name") == "only_function" for h in hits)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="the released version to install")
    ap.add_argument("--pythons", default="3.10,3.13",
                    help="minimum and newest supported, comma separated")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    pythons = [p.strip() for p in args.pythons.split(",") if p.strip()]
    if not pythons:
        print("no interpreters given, so no clean room can be built")
        return 1

    work = Path(args.work_dir) if args.work_dir else (HERE / "work")
    work.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str, str, str]] = []
    notes: list[str] = []

    for py in pythons:
        room = CleanRoom(py, args.version, work / f"py{py}")
        if not room.build() or not room.clone():
            # Every check for this interpreter is unverifiable, listed one by one: a short
            # row list would read as a short gate rather than an unrun one.
            for name in ("installed version", "six outputs", "re-index is quiet",
                         "offline run", "no-manifest repo"):
                rows.append((py, name, checks.UNVERIFIABLE, "the clean room did not build"))
            notes += [f"py{py}: {n}" for n in room.notes]
            continue
        rows.append((py, "installed version",
                     *checks.installed_version(room.reported_version(), args.version)))
        room.init()
        first, second = room.index_twice()
        rows.append((py, "six outputs", *checks.produced_outputs(room.six_outputs())))
        rows.append((py, "re-index is quiet", *checks.reindex_is_quiet(first, second)))
        refused, code, command = room.offline()
        rows.append((py, "offline run", *checks.offline_run(refused, code, command)))
        rows.append((py, "no-manifest repo", *checks.repo_without_manifest(*room.no_manifest())))
        notes += [f"py{py}: {n}" for n in room.notes]
        if not args.keep:
            shutil.rmtree(room.tree, ignore_errors=True)

    mark = {checks.VERIFIED: "ok  ", checks.BROKEN: "FAIL", checks.UNVERIFIABLE: "????"}
    for py, name, status, detail in rows:
        print(f"  [{mark[status]}] py{py} {name} -- {detail}")
    ok, line = checks.summarise(rows)
    print(line)
    for n in notes:
        print("  note:", n)

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "cleanroom.json").write_text(json.dumps({
        "version": args.version, "pythons": args.pythons,
        "tree": {"url": TREE_URL, "ref": TREE_REF, "commit": TREE_COMMIT},
        "rows": [{"python": p, "check": c, "status": s, "detail": d} for p, c, s, d in rows],
        "ok": ok, "notes": notes,
    }, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {RESULTS / 'cleanroom.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
