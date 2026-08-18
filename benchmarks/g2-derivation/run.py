#!/usr/bin/env python3
"""Prove the six output types are GENERATED from source, not merely emitted after it.

Every one of them could be satisfied by a fixture, a cached sample, or by reading a README.
"It appeared after indexing" says nothing about where it came from. So this clones a pinned
public tree, measures each output, changes ONE thing in the tree, re-indexes, measures
again, and asserts the specific movement that change implies.

The bars are in `bars.json`, written before the test rather than after it, each naming a
failure it would catch. The verdicts are in `checks.py`, separated so the deciding half can
be tested without a network -- see `tests/test_g2_derivation_checks.py`.

Not run in CI: it needs the network and minutes of indexing. Meant to be run by hand, by
anyone, producing a file they can diff against the committed one.

    python benchmarks/g2-derivation/run.py
    python benchmarks/g2-derivation/run.py --keep   # leave the work tree for inspection

Everything it writes lives under `--work-dir` and `results/`. Every contextlake invocation
carries an explicit `--config` pointing inside the work directory, so a run can never touch
a store the operator already has.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
sys.path.insert(0, str(HERE))
import checks  # noqa: E402 - deliberately after the path insert


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False,
                          timeout=timeout)


class Harness:
    def __init__(self, work: Path, spec: dict):
        self.work = work
        self.spec = spec
        self.tree = work / "tree"
        self.store = work / "store"
        self.config = work / "kb.toml"
        self.cli = [sys.executable, "-m", "contextlake"]
        self.notes: list[str] = []

    # --- setup ---------------------------------------------------------------

    def clone(self) -> bool:
        t = self.spec["tree"]
        if self.tree.exists():
            shutil.rmtree(self.tree)
        r = run(["git", "clone", "--quiet", t["url"], str(self.tree)], timeout=900)
        if r.returncode != 0:
            self.notes.append(f"clone failed: {r.stderr.strip()[-200:]}")
            return False
        r = run(["git", "checkout", "--quiet", t["commit"]], cwd=self.tree)
        if r.returncode != 0:
            # Pinned, because an unpinned tree makes every number here unreproducible.
            self.notes.append(f"pinned commit {t['commit'][:12]} not found: "
                              f"{r.stderr.strip()[-200:]}")
            return False
        self.config.write_text(
            f'[kb]\nstore_dir = "{self.store}"\n\n[embeddings]\nenabled = true\n',
            encoding="utf-8")
        return True

    def cl(self, *args: str, timeout: int = 1800):
        # `--config` on every single invocation: without it a run writes into whatever
        # store the operator's own config points at.
        return run([*self.cli, "--config", str(self.config), *args], timeout=timeout)

    def index(self) -> bool:
        r = self.cl("kb", "index", str(self.tree))
        if r.returncode != 0:
            self.notes.append(f"index failed: {(r.stderr or r.stdout).strip()[-200:]}")
            return False
        return True

    # --- the probe -----------------------------------------------------------

    def apply_probe(self) -> int:
        """Add one function and call it from several places; add one runtime dependency.

        Returns the number of call sites written, which the API-reference and diagram bars
        both assert against -- a symbol listed without its real call sites is a name, not a
        reference.
        """
        p = self.spec["probe"]
        pkg = next((d for d in sorted(self.tree.glob("src/*/__init__.py"))), None)
        target = pkg.parent if pkg else self.tree
        probe = target / "_g2_probe.py"
        sites = 5
        body = [f'"""{p["docstring"]}"""', "", "", f'def {p["symbol"]}(value):',
                f'    """{p["docstring"]}"""', "    return value", "", ""]
        for i in range(sites):
            body += [f"def _g2_call_{i}():", f"    return {p['symbol']}({i})", ""]
        probe.write_text("\n".join(body), encoding="utf-8")

        manifest = self.tree / "pyproject.toml"
        if manifest.exists():
            text = manifest.read_text(encoding="utf-8")
            if "dependencies = [" in text:
                text = text.replace("dependencies = [",
                                    f'dependencies = [\n    "{p["dependency"]}",', 1)
            else:
                text = text.replace("[project]",
                                    f'[project]\ndependencies = ["{p["dependency"]}"]', 1)
            manifest.write_text(text, encoding="utf-8")
        else:
            self.notes.append("no pyproject.toml: the design-notes and fleet bars cannot "
                              "be tested on this tree")
        run(["git", "add", "-A"], cwd=self.tree)
        run(["git", "-c", "user.email=g2@example.invalid", "-c", "user.name=g2",
             "commit", "--quiet", "-m", "g2 derivation probe"], cwd=self.tree)
        return sites

    # --- measurement ---------------------------------------------------------

    def measure(self) -> dict:
        """Every number the bars compare. A value this cannot read stays absent from the
        dict rather than defaulting, so `checks` can tell "not measured" from "measured
        and wrong" -- the distinction the whole harness turns on."""
        m: dict = {}
        p = self.spec["probe"]
        repo = self.spec["tree"]["key"]

        lint = self.cl("kb", "lint", "--json")
        if lint.returncode in (0, 1) and lint.stdout.strip():
            try:
                d = json.loads(lint.stdout)
                m["nodes"] = d.get("nodes")
                m["edges"] = d.get("edges")
                m["dangling"] = d.get("dangling")
            except json.JSONDecodeError:
                self.notes.append("kb lint --json did not return JSON")

        self.cl("kb", "docs")
        api = next(iter(sorted(self.store.glob(f"docs/api/*{repo}*.md"))), None)
        if api is not None:
            text = api.read_text(encoding="utf-8")
            m["api_has_symbol"] = p["symbol"] in text
            m["api_call_sites"] = len(re.findall(r"_g2_call_\d+", text))
            n = re.search(r"(\d+)\s+symbol", text)
            m["api_symbols"] = int(n.group(1)) if n else None
        design = next(iter(sorted(self.store.glob(f"docs/design/*{repo}*.md"))), None)
        if design is not None:
            text = design.read_text(encoding="utf-8")
            m["design_has_dep"] = p["dependency"] in text
            line = re.search(rf"{p['dependency']}.*?:(\d+)", text)
            m["design_dep_line"] = int(line.group(1)) if line else None
            m["design_adrs"] = len(re.findall(r"^#+\s*ADR-\d+", text, re.M))
        fleet = next(iter(sorted(self.store.glob("docs/fleet/*.md"))), None)
        if fleet is not None:
            text = fleet.read_text(encoding="utf-8")
            shared = re.search(r"(\d+)\s+of\s+\d+", text)
            m["fleet_shared"] = int(shared.group(1)) if shared else None
            row = re.search(rf"{p['dependency']}.*?(\d+)\s*repo", text)
            m["fleet_dep_repos"] = int(row.group(1)) if row else 0

        out = self.work / "diagram.mmd"
        g = self.cl("kb", "graph", "--name", p["symbol"], "--format", "mermaid",
                    "--output", str(out))
        shown = (g.stdout or "") + (g.stderr or "")
        printed = re.search(r"(\d+)\s+nodes?,\s*(\d+)\s+edges?", shown)
        if printed:
            m["diagram_nodes"] = int(printed.group(1))
            m["diagram_edges"] = int(printed.group(2))
        if out.exists():
            body = out.read_text(encoding="utf-8")
            m["diagram_rendered_nodes"] = len(re.findall(r"^\s{4}\w+\[", body, re.M))
            m["diagram_rendered_edges"] = len(re.findall(r"-->", body))

        self.cl("kb", "wiki")
        page = next(iter(sorted(self.store.glob("wiki/*.md"))), None)
        if page is not None:
            stamp = re.search(r"\b([0-9a-f]{7,40})\b", page.read_text(encoding="utf-8"))
            m["wiki_commit"] = stamp.group(1) if stamp else None

        self.cl("kb", "embed")
        q = self.cl("kb", "query", p["docstring"], "--retriever", "semantic", "--json")
        if q.returncode == 0 and q.stdout.strip():
            try:
                hits = json.loads(q.stdout)
                names = [h.get("name") for h in hits] if isinstance(hits, list) else []
                m["semantic_rank"] = names.index(p["symbol"]) if p["symbol"] in names else None
            except json.JSONDecodeError:
                self.notes.append("kb query --json did not return JSON")
        return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work-dir", default=str(Path.home() / "Work/ContextLake/playground/g2"))
    ap.add_argument("--keep", action="store_true", help="leave the work tree in place")
    args = ap.parse_args(argv)

    spec = json.loads((HERE / "bars.json").read_text(encoding="utf-8"))
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    h = Harness(work, spec)

    if not h.clone() or not h.index():
        # Reported as a failed RUN, never as passing bars: a harness that could not start
        # has proven nothing, and this is the one place that mistake would be invisible.
        print("run did not start:")
        for n in h.notes:
            print("  ", n)
        return 1
    before = h.measure()
    sites = h.apply_probe()
    if not h.index():
        print("re-index after the probe failed:", *h.notes, sep="\n  ")
        return 1
    after = h.measure()

    p = spec["probe"]
    rows = [
        ("code graph", *checks.code_graph(before, after)),
        ("generated docs (API reference)",
         *checks.api_reference(before, after, symbol=p["symbol"], call_sites=sites)),
        ("generated docs (design notes)",
         *checks.design_notes(before, after, dependency=p["dependency"])),
        ("fleet view", *checks.fleet_view(before, after, dependency=p["dependency"])),
        ("diagrams", *checks.diagram(before, after, call_sites=sites)),
        ("wiki", *checks.wiki(before, after)),
        ("vector search", *checks.vector_search(before, after, symbol=p["symbol"])),
    ]
    mark = {checks.VERIFIED: "ok  ", checks.BROKEN: "FAIL", checks.UNVERIFIABLE: "????"}
    for name, status, detail in rows:
        print(f"  [{mark[status]}] {name} -- {detail}")
    ok, line = checks.summarise(rows)
    print(line)
    for n in h.notes:
        print("  note:", n)

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "derivation.json").write_text(json.dumps({
        "tree": spec["tree"], "call_sites": sites, "before": before, "after": after,
        "rows": [{"output": n, "status": s, "detail": d} for n, s, d in rows],
        "ok": ok, "notes": h.notes,
    }, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {RESULTS / 'derivation.json'}")

    if not args.keep:
        shutil.rmtree(h.tree, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
