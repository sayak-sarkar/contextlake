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
    """Everything that touches disk, a subprocess, or the network.

    Split from `checks.py` so the deciding half can be tested without either. The split is
    not enough on its own, and this file is the proof: a first version's measurement regexes
    were written against what the outputs were ASSUMED to look like, and five of seven bars
    could never have passed while three would have reported the product broken. Every
    pattern below is now anchored on something the product deliberately emits -- the
    `contextlake:generated` marker, a command's own summary line -- and was checked against
    real output from a real run, which is what `--tree-path` exists for.
    """

    def __init__(self, work: Path, spec: dict, tree_path: Path | None = None):
        self.work = work
        self.spec = spec
        self.local_tree = tree_path
        self.tree = tree_path or (work / "tree")
        # A SECOND repository, because "shared across the fleet" is defined as more than one
        # repo declaring the same package. With one repo indexed the fleet bar could never
        # pass, whatever the code did -- a bar that cannot pass tests nothing.
        self.peer = work / "peer"
        self.store = work / "store"
        self.config = work / "kb.toml"
        self.cli = [sys.executable, "-m", "contextlake"]
        self.repo_id = self.tree.name
        self.notes: list[str] = []

    # --- setup ---------------------------------------------------------------

    def prepare(self) -> bool:
        if self.local_tree is None and not self._clone():
            return False
        self.repo_id = self.tree.name
        self.config.write_text(
            f'[kb]\nstore_dir = "{self.store}"\n\n[embeddings]\nenabled = true\n',
            encoding="utf-8")
        return self._make_peer()

    def _clone(self) -> bool:
        t = self.spec["tree"]
        if self.tree.exists():
            shutil.rmtree(self.tree)
        r = run(["git", "clone", "--quiet", t["url"], str(self.tree)], timeout=900)
        if r.returncode != 0:
            self.notes.append(f"clone failed: {r.stderr.strip()[-200:]}")
            return False
        # Pinned, because an unpinned tree makes every number here unreproducible.
        r = run(["git", "checkout", "--quiet", t["commit"]], cwd=self.tree)
        if r.returncode != 0:
            self.notes.append(f"pinned commit {t['commit'][:12]} not found: "
                              f"{r.stderr.strip()[-200:]}")
            return False
        return True

    def _make_peer(self) -> bool:
        """A minimal second repository that already declares the probe dependency.

        It exists only so the fleet page has a fleet: the shared-package count is defined
        over repositories, so adding the dependency to the tree under test moves that count
        from "required by exactly one" to "shared by two" -- which is the movement the fleet
        bar asserts.
        """
        dep = self.spec["probe"]["dependency"]
        if self.peer.exists():
            shutil.rmtree(self.peer)
        (self.peer / "src" / "peer_pkg").mkdir(parents=True)
        (self.peer / "src" / "peer_pkg" / "__init__.py").write_text(
            "def peer_entry():\n    return 1\n", encoding="utf-8")
        (self.peer / "pyproject.toml").write_text(
            f'[project]\nname = "peer-pkg"\nversion = "0.1.0"\n'
            f'dependencies = ["{dep}"]\n', encoding="utf-8")
        for cmd in (["git", "init", "-q", "."], ["git", "add", "-A"],
                    ["git", "-c", "user.email=g2@example.invalid", "-c", "user.name=g2",
                     "commit", "-qm", "peer"]):
            r = run(cmd, cwd=self.peer)
            if r.returncode != 0:
                self.notes.append(f"peer repo setup failed: {r.stderr.strip()[-160:]}")
                return False
        return True

    def cl(self, *args: str, timeout: int = 1800):
        # `--config` on every single invocation: without it a run writes into whatever
        # store the operator's own config points at.
        return run([*self.cli, "--config", str(self.config), *args], timeout=timeout)

    def index(self) -> tuple[int | None, int | None]:
        """Index both repositories; return the tree's own `(nodes, edges)`.

        Read from the command's own summary line rather than from `kb lint --json`, which
        reports repos/checked/stale/dangling and has no node or edge count at all. A first
        version asked lint for `nodes` and got nothing, so the code-graph bar was
        permanently unverifiable while looking like it was being tested.
        """
        self.cl("kb", "index", str(self.peer))
        r = self.cl("kb", "index", str(self.tree))
        if r.returncode != 0:
            self.notes.append(f"index failed: {(r.stderr or r.stdout).strip()[-200:]}")
            return None, None
        m = re.search(rf"Indexed {re.escape(self.repo_id)}: (\d+) nodes?, (\d+) edges?",
                      (r.stdout or "") + (r.stderr or ""))
        if not m:
            self.notes.append("could not read the node/edge counts from `kb index` output")
            return None, None
        return int(m.group(1)), int(m.group(2))

    # --- the probe -----------------------------------------------------------

    def apply_probe(self) -> int:
        """Add one function called from several places, and one runtime dependency."""
        p = self.spec["probe"]
        pkg = next(iter(sorted(self.tree.glob("src/*/__init__.py"))), None)
        target = pkg.parent if pkg else self.tree
        sites = 5
        body = [f'"""{p["module_doc"]}"""', "", "", f'def {p["symbol"]}(value):',
                f'    """{p["docstring"]}"""', "    return value", "", ""]
        for i in range(sites):
            body += [f"def _g2_call_{i}():", f"    return {p['symbol']}({i})", ""]
        (target / "_g2_probe.py").write_text("\n".join(body), encoding="utf-8")

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
            self.notes.append("no pyproject.toml in the tree: the design-notes and fleet "
                              "bars cannot be tested on it")
        run(["git", "add", "-A"], cwd=self.tree)
        run(["git", "-c", "user.email=g2@example.invalid", "-c", "user.name=g2",
             "commit", "--quiet", "-m", "g2 derivation probe"], cwd=self.tree)
        return sites

    # --- measurement ---------------------------------------------------------

    @staticmethod
    def _stamp(text: str) -> str | None:
        """The commit out of the marker the product writes for exactly this purpose."""
        m = re.search(r"contextlake:generated[^>]*commit=([0-9a-f]{7,40})", text)
        return m.group(1) if m else None

    def _doc(self, kind: str) -> str | None:
        path = self.store / "docs" / kind / f"{self.repo_id}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def measure(self, nodes: int | None, edges: int | None) -> dict:
        """Every number the bars compare.

        A value this cannot read is LEFT OUT of the dict, never written as None or 0. That
        is the whole reason `checks.py` distinguishes an absent key from an unreadable
        value: writing a default here would turn "the harness could not look" into "the
        product is broken", which is the accusation this file must never make by accident.
        """
        m: dict = {}
        p = self.spec["probe"]
        if nodes is not None:
            m["nodes"] = nodes
        if edges is not None:
            m["edges"] = edges

        lint = self.cl("kb", "lint", "--json")
        if lint.stdout.strip():
            try:
                m["dangling"] = json.loads(lint.stdout).get("dangling")
            except json.JSONDecodeError:
                self.notes.append("kb lint --json did not return JSON")

        self.cl("kb", "docs")
        api = self._doc("api")
        if api is not None:
            m["api_has_symbol"] = f"`{p['symbol']}(" in api
            total = re.search(r"\d+ of (\d+) callable symbols", api)
            if total:
                m["api_symbols"] = int(total.group(1))
            # The probe's OWN section, not the whole page: the callers are documented
            # symbols too, so counting `_g2_call_N` across the file counts each of them
            # twice -- once as a call site and once as its own heading.
            section = re.search(rf"### `{re.escape(p['symbol'])}\(.*?(?=\n### |\Z)", api,
                                re.S)
            if section:
                m["api_call_sites"] = len(re.findall(r"_g2_call_\d+", section.group(0)))

        design = self._doc("design")
        if design is not None:
            m["design_has_dep"] = f"`{p['dependency']}`" in design
            # `pyproject.toml:4` declares `urllib3` -- the line number PRECEDES the name.
            line = re.search(rf"`[^`]*?:(\d+)` declares `{re.escape(p['dependency'])}`",
                             design)
            if line:
                m["design_dep_line"] = int(line.group(1))
            elif m["design_has_dep"]:
                m["design_dep_line"] = None   # present, and no line: a real defect
            m["design_adrs"] = len(re.findall(r"^### ADR-\d+", design, re.M))

        fleet_path = self.store / "docs" / "fleet" / "design.md"
        if fleet_path.exists():
            text = fleet_path.read_text(encoding="utf-8")
            # The page's own sentence, quoted from a real run: "1 of 2 packages required
            # at runtime are required by more than one repository."
            shared = re.search(
                r"(\d+) of \d+ packages required at runtime are required by more than one",
                text)
            if shared:
                m["fleet_shared"] = int(shared.group(1))
            elif "Nothing is shared" in text:
                m["fleet_shared"] = 0
            row = re.search(rf"`{re.escape(p['dependency'])}`[^\n]*?\|\s*(\d+)\s*\|", text)
            if row:
                m["fleet_dep_repos"] = int(row.group(1))
            elif f"`{p['dependency']}`" in text:
                m["fleet_dep_repos"] = 1

        out = self.work / "diagram.mmd"
        g = self.cl("kb", "graph", "--name", p["symbol"], "--format", "mermaid",
                    "--output", str(out))
        printed = re.search(r"\((\d+) nodes?, (\d+) edges?\)",
                            (g.stdout or "") + (g.stderr or ""))
        if printed:
            m["diagram_nodes"] = int(printed.group(1))
            m["diagram_edges"] = int(printed.group(2))
        if out.exists():
            body = out.read_text(encoding="utf-8")
            # Two-space indent, which is what `to_mermaid` writes. A first version looked
            # for four and counted zero nodes against an announced 38, which the diagram
            # bar would have reported as the product drawing a different graph than it
            # described -- a false accusation from a miscounted space.
            m["diagram_rendered_nodes"] = len(re.findall(r"^\s{2}\w+\[", body, re.M))
            m["diagram_rendered_edges"] = len(re.findall(r"-->", body))

        self.cl("kb", "wiki")
        page = self.store / "wiki" / f"{self.repo_id}.md"
        if page.exists():
            text = page.read_text(encoding="utf-8")
            stamp = self._stamp(text) or (
                lambda x: x.group(1) if x else None)(
                    re.search(r"at commit `([0-9a-f]{7,40})`", text))
            if stamp:
                m["wiki_commit"] = stamp
            kinds = re.search(r"(\d+)\s+function", text)
            if kinds:
                m["wiki_functions"] = int(kinds.group(1))

        self.cl("kb", "embed")
        q = self.cl("kb", "query", p["semantic_query"], "--retriever", "semantic", "--json")
        shown = (q.stdout or "") + (q.stderr or "")
        if "nothing indexed matches" in shown:
            # The relevance floor refused the query: none of its terms is in this graph, so
            # the retriever was never asked. Recording that as "the symbol was not returned"
            # would accuse the product of a ranking failure for correct behaviour -- the
            # same "could not ask, reported as failed" the harness exists to catch. A tree
            # whose vocabulary does not carry the query cannot test this bar.
            self.notes.append(
                "the relevance floor refused the semantic query (no term in it is indexed "
                "in this tree), so the vector bar was not tested -- it needs a tree whose "
                "vocabulary supports the question")
        elif "Showing fts results instead" in shown or "cannot answer" in shown:
            # The retriever degraded, and full-text search matches this query's words. A
            # lexical hit reported as a semantic one is precisely the failure this bar
            # exists to catch, so it is recorded as not-tested rather than as a pass.
            self.notes.append("semantic search degraded to full-text; the vector bar was "
                              "not tested")
        elif q.stdout.strip():
            try:
                hits = json.loads(q.stdout)
                names = [h.get("name") for h in hits] if isinstance(hits, list) else []
                m["semantic_rank"] = names.index(p["symbol"]) if p["symbol"] in names else None
            except json.JSONDecodeError:
                self.notes.append("kb query --json did not return JSON")
        return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work-dir", default=None,
                    help="scratch directory (default: a g2 directory beside this file's "
                         "results, so the benchmark is runnable by anyone)")
    ap.add_argument("--tree-path", default=None,
                    help="measure a LOCAL tree instead of cloning the pinned one. The "
                         "committed evidence uses the pinned public tree; this exists so "
                         "the measurement layer can be exercised without a network, which "
                         "is how its patterns are kept honest")
    ap.add_argument("--keep", action="store_true", help="leave the work tree in place")
    args = ap.parse_args(argv)

    spec = json.loads((HERE / "bars.json").read_text(encoding="utf-8"))
    work = Path(args.work_dir) if args.work_dir else (HERE / "work")
    work.mkdir(parents=True, exist_ok=True)
    tree = Path(args.tree_path).resolve() if args.tree_path else None
    h = Harness(work, spec, tree_path=tree)

    if not h.prepare():
        # Reported as a failed RUN, never as passing bars: a harness that could not start
        # has proven nothing, and this is the one place that mistake would be invisible.
        print("run did not start:", *h.notes, sep="\n  ")
        return 1
    nodes, edges = h.index()
    before = h.measure(nodes, edges)
    sites = h.apply_probe()
    nodes, edges = h.index()
    after = h.measure(nodes, edges)

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
    name = "derivation-local.json" if tree else "derivation.json"
    (RESULTS / name).write_text(json.dumps({
        "tree": str(tree) if tree else spec["tree"], "call_sites": sites,
        "before": before, "after": after,
        "rows": [{"output": n, "status": s, "detail": d} for n, s, d in rows],
        "ok": ok, "notes": h.notes,
    }, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {RESULTS / name}")

    if not args.keep and tree is None:
        shutil.rmtree(h.tree, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
