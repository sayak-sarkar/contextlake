#!/usr/bin/env python3
"""Tier E: output oracles on a multi-repo, multi-language fixture (stability campaign v2).

v1 (tier3_oracles.py) ran six oracles on one Python repo. This tier runs the same six
families, plus SQL, file-id, ADR, manifest and MCP-contract oracles, on the four-repo
fixture in `fixture_e.py`. Every oracle has a NAMED expected value, and every oracle is
break-tested on every run: the same comparison is run again with a deliberately wrong
expected value, and it must fire. An oracle whose break-test does not fire is blind and is
reported as a harness defect, never as a pass.

Audit IDs this tier targets: W01 (SQL `IF NOT EXISTS`), D05 (file ids that normalise the
same), W03 (MCP fields the server instructions promise). Rows tagged `W01-consequence` miss
because of W01 and are not counted as separate catches.

This module does not run anything by itself. Every contextlake call goes through a run
function the caller passes to ``main`` (and on to ``Ctx``), with the signature and result
shape of ``ci_runner.run``. Two callers exist:

- A private wrapper, which passes a guarded runner for a developer machine.
- This file run as a script, which uses ``ci_runner`` and works only inside GitHub Actions
  (tier F: the same oracles on a Linux and a Windows cell)::

      python scripts/stability/tier_e.py --target 9.8.3 --portable \\
          --venv VENV --root ROOT --out OUT --hf-cache HF_HUB_CACHE

Raw output and ``results.json`` go to ``<out>/<tag>/``. The tag defaults to
``<target>[-portable]``. The rows mark which oracles are portable.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# First on sys.path, so this directory's fixture_e and ci_runner win over any older copy
# that sits next to a wrapper script.
sys.path.insert(0, str(HERE))
import fixture_e  # noqa: E402  (portable: no runner import)

if Path(fixture_e.__file__).resolve().parent != HERE:
    raise ImportError(f"fixture_e came from {fixture_e.__file__}, not from {HERE}")

MODEL = "models--minishlab--potion-base-8M"
CONF = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}
CIT = {"verified", "stale", "unverifiable"}
NEVER = {"__never__"}

# ======================================================================================
# Result rows and the break-test
# ======================================================================================
ROWS: list[dict] = []


def judge(oid: str, title: str, *, audit: str, expected, actual, check, broken,
          evidence: list[str], portable: bool = True, note: str = "") -> dict:
    """Record one oracle row. ``check(actual, expected)`` returns failure lines.

    The break-test runs ``check(actual, broken)``. It must return at least one line. An
    exception there is recorded as an error, never as a fire.
    """
    try:
        fails = check(actual, expected)
        harness_error = None
    except Exception as e:  # noqa: BLE001
        fails, harness_error = [], f"{type(e).__name__}: {e}"
    try:
        bfails = check(actual, broken)
        bt = ("fired: " + bfails[0]) if bfails else "DID NOT FIRE"
    except Exception as e:  # noqa: BLE001
        bt = f"error, not a fire: {type(e).__name__}: {e}"
    row = {"oid": oid, "title": title, "audit": audit, "portable": portable,
           "status": "ERROR" if harness_error else ("FAIL" if fails else "PASS"),
           "fired": fails[:8], "n_fired": len(fails), "harness_error": harness_error,
           "breaktest": bt[:300], "expected": _short(expected), "actual": _short(actual),
           "evidence": evidence, "note": note}
    ROWS.append(row)
    mark = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR "}[row["status"]]
    print(f"  {mark} {oid:44} {('[' + audit + ']') if audit else '':18} "
          f"{(fails[0] if fails else (harness_error or ''))[:110]}")
    return row


def na(oid: str, title: str, *, audit: str, reason: str, evidence: list[str],
       portable: bool = True) -> None:
    ROWS.append({"oid": oid, "title": title, "audit": audit, "portable": portable,
                 "status": "N/A", "fired": [], "n_fired": 0, "harness_error": None,
                 "breaktest": "n/a", "expected": "", "actual": "", "evidence": evidence,
                 "note": reason})
    print(f"  N/A  {oid:44} {('[' + audit + ']') if audit else '':18} {reason[:110]}")


def _short(v, n: int = 600) -> str:
    s = json.dumps(v, default=lambda o: sorted(o) if isinstance(o, set) else str(o))
    return s if len(s) <= n else s[:n] + "...(cut)"


# ======================================================================================
# Comparisons (pure; portable)
# ======================================================================================
def _matches(entry: dict, subset: dict) -> bool:
    return all(entry.get(k) == v for k, v in subset.items())


def ck_contains(entries, subsets) -> list[str]:
    """Every expected subset matches at least one entry."""
    if entries is None:
        return ["no parseable output"]
    return [f"missing entry {s}" for s in subsets if not any(_matches(e, s) for e in entries)]


def ck_top(entries, subset) -> list[str]:
    if not entries:
        return ["no hits"]
    top = entries[0]
    return [f"top hit {k}={top.get(k)!r}, expected {v!r}" for k, v in subset.items()
            if top.get(k) != v]


def ck_eq(actual, expected) -> list[str]:
    return [] if actual == expected else [
        f"got {_short(actual, 200)}, expected {_short(expected, 200)}"]


def ck_set_eq(actual, expected) -> list[str]:
    if actual is None:
        return ["no parseable output"]
    a, e = set(actual), set(expected)
    out = [f"unexpected {x}" for x in sorted(a - e, key=str)]
    out += [f"missing {x}" for x in sorted(e - a, key=str)]
    return out


def ck_superset(actual, expected) -> list[str]:
    if actual is None:
        return ["no parseable output"]
    return [f"missing {x}" for x in sorted(set(expected) - set(actual), key=str)]


def ck_disjoint(actual, forbidden) -> list[str]:
    if actual is None:
        return ["no parseable output"]
    return [f"forbidden value present: {x}" for x in sorted(set(actual) & set(forbidden), key=str)]


def ck_subset(obj, expected: dict) -> list[str]:
    """Every expected key is present in ``obj`` with the expected value."""
    if not isinstance(obj, dict):
        return ["no parseable output"]
    return [f"`{k}`={obj.get(k, '<absent>')!r}, expected {v!r}" for k, v in expected.items()
            if obj.get(k, "<absent>") != v]


def ck_mentions(text, needles) -> list[str]:
    if not isinstance(text, str):
        return [f"no text (got {text!r})"]
    return [f"{text!r} does not mention {n!r}" for n in needles if n not in text]


def ck_rank_delta(actual, min_gain) -> list[str]:
    """actual = (rank with docstring, rank without, result count). Lower rank is better."""
    with_doc, without_doc, _n = actual
    if with_doc is None or without_doc is None:
        return [f"target not ranked (with={with_doc}, without={without_doc})"]
    gain = without_doc - with_doc
    return [] if gain >= min_gain else [
        f"docstring moved rank {without_doc} -> {with_doc} (gain {gain}), "
        f"expected gain >= {min_gain}"]


_PRED = {
    "conf": lambda v: v in CONF,
    "cit": lambda v: v in CIT,
    "verified": lambda v: v == "verified",
    "nonnull": lambda v: v is not None and v != "",
    "int>=1": lambda v: isinstance(v, int) and v >= 1,
    "number": lambda v: isinstance(v, (int, float)),
    "null": lambda v: v is None,
    "never": lambda v: False,
}


def ck_entries(entries, spec) -> list[str]:
    """spec = {"min": n, "fields": {field: pred}, "when_file": bool}.

    Every entry must carry every field and pass its predicate. A missing KEY is reported as
    missing, apart from a null value. ``when_file`` limits the check to entries that cite
    a file (the instructions' "cited node").
    """
    if entries is None:
        return ["no parseable output"]
    out = []
    if len(entries) < spec.get("min", 1):
        out.append(f"{len(entries)} entries, expected at least {spec.get('min', 1)}")
    if spec.get("when_file") and entries and not any(e.get("file") for e in entries):
        out.append("no entry cites a file, so the promise was not exercised")
    for i, e in enumerate(entries):
        if spec.get("when_file") and not e.get("file"):
            continue
        who = e.get("name") or e.get("id") or e.get("dst") or i
        for f, pred in spec["fields"].items():
            if f not in e:
                out.append(f"entry {i} ({who}): `{f}` is absent")
            elif not _PRED[pred](e[f]):
                out.append(f"entry {i} ({who}): `{f}`={e[f]!r} fails {pred}")
    return out


def ck_field(obj, spec) -> list[str]:
    """spec = {field: pred} on one object."""
    if obj is None:
        return ["no parseable output"]
    out = []
    for f, pred in spec.items():
        if f not in obj:
            out.append(f"`{f}` is absent")
        elif not _PRED[pred](obj[f]):
            out.append(f"`{f}`={obj[f]!r} fails {pred}")
    return out


def broken_spec(spec: dict) -> dict:
    """The break-test value for ck_entries/ck_field: every predicate becomes impossible."""
    if "fields" in spec:
        return {**spec, "fields": {f: "never" for f in spec["fields"]}}
    return {f: "never" for f in spec}


def first_json(text: str):
    """The first JSON object or array in ``text`` (logs may precede it)."""
    if not text:
        return None
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            return dec.raw_decode(text[m.start():])[0]
        except json.JSONDecodeError:
            continue
    return None


# ======================================================================================
# Execution context. The caller supplies the run function, the target and the root.
# ======================================================================================
class Ctx:
    """One run of tier E against one installed contextlake.

    ``run`` has the signature of ``ci_runner.run``. ``target`` is what ``run`` takes as its
    first argument and has a ``name``. ``root`` is the working root (fixtures, stores,
    configs) and ``raw`` the results root; both get a ``<tag>`` subdirectory, which is
    deleted first if it exists. ``hf_cache`` is the Hugging Face hub cache that holds the
    embedding model.
    """

    def __init__(self, run, target, root: Path, raw: Path, portable: bool,
                 tag: str | None = None, hf_cache: Path | None = None):
        self.run = run
        self.t = target
        self.target = target.name
        self.portable = portable
        self.tag = tag or (self.target + ("-portable" if portable else ""))
        self.root = Path(root) / self.tag
        self.raw = Path(raw) / self.tag
        self.hf_cache = Path(hf_cache) if hf_cache else Path.home() / ".cache/huggingface/hub"
        for d in (self.root, self.raw):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
        self.home, self.cwd, self.stubs = self.root / "home", self.root / "cwd", self.root / "stubs"
        for d in (self.home, self.cwd):
            d.mkdir()
        self.seq = 0
        self.cfg: dict[str, Path] = {}
        self.env: dict[str, dict] = {}
        self.calls: list[dict] = []

    def cl(self, label: str, args: list[str], *, cfg: str = "main", timeout: float = 300,
           exe: str | None = None) -> tuple[object, str]:
        """Run contextlake (or ``exe`` from the venv) and log the raw result. Returns (r, ref)."""
        argv = list(args) if exe else [*args, "--config", str(self.cfg[cfg])]
        r = self.run(self.t, argv, home=self.home, cwd=self.cwd, stub_dir=self.stubs,
                     timeout=timeout, env=self.env.get(cfg), exe=exe)
        self.seq += 1
        ref = f"{self.seq:03d}-{re.sub(r'[^A-Za-z0-9._-]+', '_', label)[:60]}"
        rec = {"label": label, "argv": r.argv, "rc": r.rc, "seconds": round(r.seconds, 2),
               "timed_out": r.timed_out, "env_keys_added": sorted(self.env.get(cfg) or {}),
               "out": r.out, "err": r.err}
        (self.raw / f"{ref}.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
        self.calls.append({"ref": ref, "argv": r.argv, "rc": r.rc})
        return r, ref

    def cl_json(self, label: str, args: list[str], **kw):
        r, ref = self.cl(label, args, **kw)
        return first_json(r.out), ref


# ======================================================================================
# Setup: three stores per target
# ======================================================================================
def setup(ctx: Ctx) -> dict:
    """main: embeddings off (every oracle but O3). emb / emb-nodoc: vectors, for O3 and the
    MCP `score` rows. Returns the git heads per workspace."""
    heads = {}
    for ws_name, docs in (("ws", True), ("ws-nodoc", False)):
        ws = ctx.root / ws_name
        fixture_e.build(ws, portable=ctx.portable, docstrings=docs)
        heads[ws_name] = fixture_e.git_init(ws)
    model_src = ctx.hf_cache / MODEL
    hf_home = ctx.home / ".cache/huggingface"
    # Content, not links, on every OS. huggingface_hub 1.33 stores a large file's blob as a
    # link into a cache-level `blobs/` directory outside the model directory, so a copy that
    # keeps links left model.safetensors and onnx/model.onnx dangling (Linux CI cell). On
    # Windows a symlink also needs a privilege the process may not hold.
    shutil.copytree(model_src, hf_home / "hub" / MODEL, symlinks=False)
    for name, ws_name, emb in (("main", "ws", False), ("emb", "ws", True),
                               ("emb-nodoc", "ws-nodoc", True)):
        cfg = ctx.root / f"kb-{name}.toml"
        store = str(ctx.root / ("store-" + name))
        if "'" in store:
            raise SystemExit(f"a TOML literal string cannot hold a quote: {store}")
        # A TOML literal string ('...'): a Windows path's backslashes are escapes in a basic
        # string ("..."), so the native path would not parse there.
        cfg.write_text(
            "[kb]\n"
            f"store_dir = '{store}'\n\n"
            "[embeddings]\n"
            f"enabled = {'true' if emb else 'false'}\n"
            + ('provider = "builtin"\n' if emb else "")
            + "\n[llm]\nenabled = false\n", encoding="utf-8")
        ctx.cfg[name] = cfg
        if emb:
            ctx.env[name] = {"HF_HOME": str(hf_home)}
        r, ref = ctx.cl(f"index-{name}", ["kb", "index", "--workspace", str(ctx.root / ws_name),
                                          "--workers", "1"], cfg=name)
        if r.rc != 0:
            raise SystemExit(f"index failed for {name}: rc={r.rc} (see {ref})")
        if emb:
            r, ref = ctx.cl(f"embed-{name}", ["kb", "embed"], cfg=name)
            if r.rc != 0:
                raise SystemExit(f"embed failed for {name}: rc={r.rc} (see {ref})")
    return heads


# ======================================================================================
# CLI oracles
# ======================================================================================
def cli_oracles(ctx: Ctx, X: dict) -> dict:
    R = X["repos"]
    bc, wa, lg, bf = R["billing-core"], R["web-app"], R["ledger"], R["billing_core"]
    lint, lint_ref = ctx.cl_json("lint", ["kb", "lint", "--json"])

    # ---- O1 repo count ---------------------------------------------------------------
    judge("O1.repo_count", "kb lint --json counts the four fixture repos", audit="",
          expected=X["repo_count"], actual=(lint or {}).get("repos"), check=ck_eq,
          broken=X["repo_count"] + 1, evidence=[lint_ref])

    # ---- O2 query top hit, one per language -------------------------------------------
    for q, f, kind in X["top_hits"]:
        hits, ref = ctx.cl_json(f"query-{q}", ["kb", "query", q, "--json"])
        judge(f"O2.top_hit.{q}", f"kb query {q}: top hit is the {kind} in {f}", audit="",
              expected={"name": q, "file": f, "kind": kind}, actual=hits, check=ck_top,
              broken={"name": q, "file": "nope/" + f, "kind": kind}, evidence=[ref])

    # ---- O3 docstring rank delta (vectors) --------------------------------------------
    for q, name, f, _repo in fixture_e.DOC_QUERIES:
        ranks, refs = [], []
        for cfg in ("emb", "emb-nodoc"):
            hits, ref = ctx.cl_json(f"semantic-{name}-{cfg}",
                                    ["kb", "query", q, "--retriever", "semantic", "--json",
                                     "--limit", "50"], cfg=cfg)
            refs.append(ref)
            names = [(h.get("name"), h.get("file")) for h in (hits or [])]
            ranks.append(names.index((name, f)) + 1 if (name, f) in names else None)
            n = len(names)
        judge(f"O3.doc_rank.{name}", f"a docstring lifts {name}'s semantic rank for '{q}'",
              audit="", expected=1, actual=(ranks[0], ranks[1], n), check=ck_rank_delta,
              broken=10_000, evidence=refs)

    # ---- O4 eval hit rate --------------------------------------------------------------
    golden_code = {"queries": [
        {"query": q, "expected": [q], "match": "name", "kind": k} for q, _f, k in X["top_hits"]]
        + [{"query": "compute_tax", "expected": ["compute_tax"], "match": "name",
            "kind": "function"}]}
    golden_sql = {"queries": [
        {"query": "orders", "expected": ["orders"], "match": "name", "kind": "table", "repo": bc},
        {"query": "invoices", "expected": ["invoices"], "match": "name", "kind": "table",
         "repo": bc},
        {"query": "sessions", "expected": ["sessions"], "match": "name", "kind": "table",
         "repo": wa}]}
    for label, golden, audit in (("code", golden_code, ""), ("sql", golden_sql, "W01-consequence")):
        gp = ctx.root / f"golden-{label}.json"
        gp.write_text(json.dumps(golden, indent=1), encoding="utf-8")
        m, ref = ctx.cl_json(f"eval-{label}", ["kb", "eval", "--golden", str(gp), "--json"])
        judge(f"O4.eval.{label}", f"kb eval hit_rate on the {label} golden set", audit=audit,
              expected=1.0, actual=(m or {}).get("hit_rate"), check=ck_eq, broken=1.5,
              evidence=[ref])

    # ---- O5 impact finds the cross-file caller, one per language ---------------------
    for callee, caller_file, callers, repo in X["callers"]:
        imp, ref = ctx.cl_json(f"impact-{callee}", ["kb", "impact", callee, "--repo", repo,
                                                    "--json"])
        aff = (imp or {}).get("affected")
        judge(f"O5.impact.{callee}", f"kb impact {callee} lists {sorted(callers)} in {caller_file}",
              audit="", expected=[{"name": c, "file": caller_file, "hop": 1} for c in callers],
              actual=aff, check=ck_contains,
              broken=[{"name": c, "file": "wrong/" + caller_file, "hop": 1} for c in callers],
              evidence=[ref])

    # ---- O6 graph JSON parses and holds the seed, one per language --------------------
    for q, _f, _k in X["top_hits"]:
        g, ref = ctx.cl_json(f"graph-{q}", ["kb", "graph", "--name", q, "--format", "json"])
        names = None if not isinstance(g, dict) else [n.get("name") for n in g.get("nodes", [])]
        judge(f"O6.graph_json.{q}", f"kb graph --name {q} --format json parses and holds {q}",
              audit="", expected=[q], actual=names, check=ck_superset, broken=[q + "_x"],
              evidence=[ref])

    # ---- O7 SQL extraction: W01 and the dialect shapes --------------------------------
    w01_files = {"db/01_if_not_exists.sql", "db/07_if_not_exists_quoted.sql",
                 "db/migrations/001_sessions.sql"}
    sql_rows = [(bc, *row) for row in X["sql_defs"]] + [(wa, *row) for row in X["webapp_tables"]]
    for repo, f, kind, name in sql_rows:
        hits, ref = ctx.cl_json(f"query-sql-{name}", ["kb", "query", name, "--kind", kind,
                                                      "--repo", repo, "--json"])
        judge(f"O7.sql.{Path(f).stem}.{name}", f"{f}: {kind} `{name}` is extracted",
              audit="W01" if f in w01_files else "",
              expected=[{"file": f, "name": name, "kind": kind}], actual=hits,
              check=ck_contains, broken=[{"file": f, "name": name + "_x", "kind": kind}],
              evidence=[ref])
    for repo, label in ((bc, "billing-core"), (wa, "web-app")):
        g, ref = ctx.cl_json(f"graph-repo-{label}", ["kb", "graph", "--repo", repo,
                                                     "--format", "json"])
        nodes = (g or {}).get("nodes") if isinstance(g, dict) else None
        tv = None if nodes is None else sorted(
            (n["kind"], n["name"]) for n in nodes if n.get("kind") in ("table", "view"))
        if label == "billing-core":
            want = sorted({(k, n) for _f, k, n in X["sql_defs"]}
                          | {("table", "ledger_entries"), ("table", "ledger_accounts")})
        else:
            want = [("table", "sessions")]
        judge(f"O7.sql.set.{label}", f"kb graph --repo {label}: the exact table/view set",
              audit="W01", expected=want, actual=tv, check=ck_set_eq,
              broken=[w for w in want[1:]] + [("table", "zz_not_a_table")], evidence=[ref])
        judge(f"O7.sql.no_keyword_names.{label}",
              f"no table/view in {label} is named after a SQL keyword or schema",
              audit="W01", expected=X["sql_keyword_names"],
              actual=None if tv is None else [n for _k, n in tv], check=ck_disjoint,
              broken=[n for _k, n in (tv or [])][:1] or ["orders"], evidence=[ref])
        if label == "billing-core":
            ctx.graph_bc = nodes  # reused by the D05 witness
    # Foreign keys and code reads/writes, from kb impact on the target table.
    for repo, table, want, oid in (
            (bc, "orders", [{"name": s, "via": "references"} for s, _d in X["sql_fks"]],
             "O7.fk.into_orders"),
            (bc, "orders", [{"file": f, "via": "reads", "kind": "file"}
                            for r, f, t in X["data_reads"] if t == "orders"],
             "O7.code_reads.orders"),
            (bc, "audit_log", [{"file": f, "via": "writes", "kind": "file"}
                               for r, f, t in X["data_writes"] if t == "audit_log"],
             "O7.code_writes.audit_log"),
            (wa, "sessions", [{"file": f, "via": "writes", "kind": "file"}
                              for r, f, t in X["data_writes"] if t == "sessions"],
             "O7.code_writes.sessions")):
        imp, ref = ctx.cl_json(f"impact-{table}", ["kb", "impact", table, "--repo", repo, "--json"])
        aff = (imp or {}).get("affected")
        cons = "W01-consequence" if table in ("orders", "sessions") else ""
        judge(oid, f"kb impact {table} lists {want}", audit=cons, expected=want, actual=aff,
              check=ck_contains, broken=[{**w, "via": "zz"} for w in want], evidence=[ref])

    # ---- O8 D05: every file owns its own file node (SQLite-backed surface) ------------
    for label, repo, a, b, token in X["collisions"]:
        hits, ref = ctx.cl_json(f"query-file-{label}", ["kb", "query", token, "--kind", "file",
                                                        "--repo", repo, "--limit", "50", "--json"])
        files = None if hits is None else [h.get("file") for h in hits if h.get("repo") == repo]
        judge(f"O8.file_node.{label}", f"{a} and {b} each have a file node in {repo}",
              audit="D05", expected=[a, b], actual=files, check=ck_superset,
              broken=[a, b, a + ".zz"], evidence=[ref],
              portable=(label != "ts-case-only"),
              note=_d05_witness(ctx, repo, (a, b)))
    token, path, r1, r2 = X["cross_repo_collision"]
    found, refs = [], []
    for repo in (r1, r2):
        hits, ref = ctx.cl_json(f"query-file-cross-{repo}", ["kb", "query", token, "--kind",
                                                             "file", "--repo", repo, "--json"])
        refs.append(ref)
        found += [(h.get("repo"), h.get("file")) for h in (hits or [])]
    judge("O8.file_node.cross_repo", f"{path} has a file node in both {r1} and {r2}",
          audit="D05", expected=[(r1, path), (r2, path)], actual=found, check=ck_superset,
          broken=[(r1, path), (r2, path), (r1, path + ".zz")], evidence=refs,
          note=_d05_witness(ctx, r1, (path,), cross=r2))

    # ---- O9 kb lint reports every collision (detect-only, shipped 9.5.0) -------------
    if lint is None or "shared_file_nodes_sample" not in lint:
        na("O9.lint_shared_file_nodes", "kb lint --json reports each D05 collision",
           audit="D05-lint", evidence=[lint_ref],
           reason="this kb lint has no shared_file_nodes field (the check shipped in 9.5.0)")
    else:
        pairs = [frozenset((f["repo"], f["path"]) for f in s["files"])
                 for s in lint["shared_file_nodes_sample"]]
        want = [frozenset({(repo, a), (repo, b)}) for _l, repo, a, b, _t in X["collisions"]]
        want.append(frozenset({(r1, path), (r2, path)}))
        judge("O9.lint_shared_file_nodes", "kb lint --json reports each D05 collision",
              audit="D05-lint", expected=want, actual=pairs,
              check=lambda act, exp: [f"not reported: {sorted(w)}" for w in exp
                                      if not any(w <= p for p in act)],
              broken=want + [frozenset({(bc, "zz.py"), (bc, "zz_.py")})], evidence=[lint_ref],
              note=f"shared_file_nodes={lint.get('shared_file_nodes')}")

    # ---- O10 ADRs at several depths and conventions ------------------------------------
    for repo, f, title, token in X["adrs"]:
        hits, ref = ctx.cl_json(f"query-adr-{Path(f).stem}", ["kb", "query", token, "--kind",
                                                              "adr", "--repo", repo, "--json"])
        judge(f"O10.adr.{Path(f).parent.name}.{Path(f).stem}", f"ADR {f} -> node '{title}'",
              audit="", expected=[{"file": f, "name": title, "kind": "adr"}], actual=hits,
              check=ck_contains, broken=[{"file": f, "name": title + " (x)", "kind": "adr"}],
              evidence=[ref])

    # ---- O11 manifests: dependents per manifest, repo->repo dependencies --------------
    for repo, mf, _pub, deps in X["manifests"]:
        for dep in deps:
            imp, ref = ctx.cl_json(f"impact-pkg-{dep}", ["kb", "impact", dep, "--json"])
            aff = (imp or {}).get("affected")
            judge(f"O11.dependent.{dep}<-{mf}", f"kb impact {dep} lists {mf} in {repo}",
                  audit="", expected=[{"repo": repo, "file": mf, "via": "depends_on"}],
                  actual=aff, check=ck_contains,
                  broken=[{"repo": repo, "file": mf + ".zz", "via": "depends_on"}],
                  evidence=[ref])
    # No fabricated dependents: ledger's tools/package.json depends on the unscoped npm
    # package `acme-billing-js`, which is not `@acme/billing-js`.
    imp, ref = ctx.cl_json("impact-pkg-scoped", ["kb", "impact", "@acme/billing-js", "--json"])
    aff = (imp or {}).get("affected")
    deps_of = None if aff is None else [(a.get("repo"), a.get("file")) for a in aff]
    judge("O11.no_fabricated_dependent.@acme/billing-js",
          "kb impact @acme/billing-js lists only manifests that name @acme/billing-js",
          audit="new", expected=[(lg, "tools/package.json")], actual=deps_of, check=ck_disjoint,
          broken=[(wa, "packages/api-client/package.json")], evidence=[ref])
    ov, ref = ctx.cl_json("graph-overview", ["kb", "graph", "--overview", "--format", "json"])
    edges = None if not isinstance(ov, dict) else [
        (e["src"], e["dst"]) for e in ov.get("edges", []) if e.get("relation") == "depends_on"]
    want = [(s, d) for s, ds in X["repo_deps_out"].items() for d in ds]
    judge("O11.repo_deps.present", "kb graph --overview holds every real repo dependency",
          audit="", expected=want, actual=edges, check=ck_superset,
          broken=want + [(bf, bc)], evidence=[ref])
    judge("O11.repo_deps.no_fabricated", "kb graph --overview holds no ledger -> billing-core edge",
          audit="new", expected=[(lg, bc)], actual=edges, check=ck_disjoint,
          broken=want[:1], evidence=[ref],
          note="ledger depends on npm `acme-billing-js`; billing-core publishes `@acme/billing-js`")
    return {"lint": lint}


def _d05_witness(ctx: Ctx, repo: str, paths: tuple, cross: str | None = None) -> str:
    """Diagnostic only, never the oracle: which row the store kept for the shared id."""
    db = ctx.root / "store-main" / "index.sqlite"
    ids = {fixture_make_id(r, p) for p in paths for r in (repo, cross) if r}
    # The SQL text holds only "?" placeholders; the ids travel as bound parameters.
    sql = ("SELECT node_id, repo_id, file FROM nodes WHERE node_id IN "  # noqa: S608
           f"({','.join('?' * len(ids))})")
    try:
        # as_uri(): a "file:" URI built from a Windows path would keep its backslashes.
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        rows = con.execute(sql, sorted(ids)).fetchall()
        con.close()
    except sqlite3.Error as e:
        return f"witness unavailable: {e}"
    return "store keeps: " + "; ".join(f"{nid} -> {rid}:{f}" for nid, rid, f in rows)


def fixture_make_id(*parts: str) -> str:
    """A copy of the documented id recipe (kb/ids.py make_id), for the witness query only."""
    import unicodedata
    s = "_".join(p.strip("_.") for p in parts if p)
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


# ======================================================================================
# MCP oracles: the output contract (W03), plus second surfaces for W01 and D05
# ======================================================================================
def _plan(X: dict) -> list[dict]:
    R = X["repos"]
    bc, wa, lg = R["billing-core"], R["web-app"], R["ledger"]

    def defn(name):
        return {"tool": "find_definition", "args": {"name": name}, "path": ["nodes", 0, "id"]}

    p = [
        ("graph_stats", "graph_stats", {}),
        ("list_repos", "list_repos", {}),
        ("graph_health", "graph_health", {}),
        ("search_code.InvoiceService", "search_code", {"query": "InvoiceService"}),
        ("search_code.filter_excludes_all", "search_code",
         {"query": "InvoiceService", "kind": "zz_no_such_kind"}),
        ("find_definition.compute_tax", "find_definition", {"name": "compute_tax"}),
        ("find_definition.missing", "find_definition", {"name": "zz_no_such_symbol"}),
        ("find_definition.orders_table", "find_definition", {"name": "orders", "kind": "table"}),
        ("find_callers.compute_tax", "find_callers", {"name": "compute_tax"}),
        ("find_callers.createClient", "find_callers", {"name": "createClient"}),
        ("find_callers.PostEntry", "find_callers", {"name": "PostEntry"}),
        ("find_callees.issue_invoice", "find_callees", {"name": "issue_invoice"}),
        ("find_dependents.acme-ledger-client", "find_dependents",
         {"package": "acme-ledger-client"}),
        ("find_dependents.@acme/api-client", "find_dependents", {"package": "@acme/api-client"}),
        ("find_dependents.missing", "find_dependents", {"package": "zz-no-such-package"}),
        ("repo_dependencies.billing-core", "repo_dependencies", {"repo": bc, "direction": "out"}),
        ("repo_dependencies.web-app", "repo_dependencies", {"repo": wa, "direction": "out"}),
        ("repo_dependencies.ledger", "repo_dependencies", {"repo": lg, "direction": "out"}),
        ("repo_dependencies.missing", "repo_dependencies", {"repo": "zz/no/such/repo"}),
        ("repo_flow.billing-core", "repo_flow", {"repo": bc}),
        ("repo_event_flow.billing-core", "repo_event_flow", {"repo": bc}),
        ("blast_radius.compute_tax", "blast_radius", {"name": "compute_tax"}),
        ("blast_radius.missing", "blast_radius", {"name": "zz_no_such_symbol"}),
        ("who_knows.billing-core", "who_knows", {"repo": bc}),
        ("who_knows.missing", "who_knows", {"repo": "zz/no/such/repo"}),
        ("get_wiki.billing-core", "get_wiki", {"repo": bc}),
        ("get_generated_doc.api", "get_generated_doc", {"repo": bc, "kind": "api"}),
        ("get_generated_doc.design", "get_generated_doc", {"repo": bc, "kind": "design"}),
        ("get_fleet_doc", "get_fleet_doc", {}),
        ("get_readme.billing-core", "get_readme", {"repo": bc}),
        ("get_repo_brief.billing-core", "get_repo_brief", {"repo": bc}),
        ("get_repo_links.billing-core", "get_repo_links", {"repo": bc}),
        ("get_repo_links.missing", "get_repo_links", {"repo": "zz/no/such/repo"}),
        ("ask.callers", "ask", {"question": "who calls compute_tax"}),
        *[(f"find_definition.doc.{n}", "find_definition", {"name": n}) for n, _f, _d in X["jsdoc"]],
        ("ask.definition", "ask", {"question": "where is InvoiceService defined"}),
    ]
    plan = [{"label": lab, "tool": tool, "args": args} for lab, tool, args in p]
    plan += [
        {"label": "get_node.InvoiceService", "tool": "get_node", "args": {},
         "resolve": {"node_id": defn("InvoiceService")}},
        {"label": "get_neighbors.compute_tax", "tool": "get_neighbors", "args": {},
         "resolve": {"node_id": defn("compute_tax")}},
        {"label": "shortest_path.renderOrders->createClient", "tool": "shortest_path",
         "args": {"max_hops": 3},
         "resolve": {"src_id": defn("renderOrders"), "dst_id": defn("createClient")}},
        {"label": "shortest_path.missing", "tool": "shortest_path",
         "args": {"src_id": "zz_no_such_node", "max_hops": 3},
         "resolve": {"dst_id": defn("createClient")}},
    ]
    for label, repo, _a, _b, token in X["collisions"]:
        plan.append({"label": f"search_code.file.{label}", "tool": "search_code",
                     "args": {"query": token, "kind": "file", "repo": repo, "limit": 50}})
    return plan


def _emb_plan() -> list[dict]:
    q = fixture_e.DOC_QUERIES[0][0]
    return [{"label": "semantic_search", "tool": "semantic_search", "args": {"query": q, "k": 10}},
            {"label": "hybrid_search", "tool": "hybrid_search", "args": {"query": q, "k": 10}}]


def run_mcp(ctx: Ctx, cfg: str, plan: list[dict]) -> tuple[dict | None, str]:
    plan_path = ctx.root / f"mcp-plan-{cfg}.json"
    plan_path.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    out = ctx.raw / f"mcp-{cfg}.json"
    r, ref = ctx.cl(f"mcp-{cfg}", [str(HERE / "mcp_client_e.py"), str(ctx.cfg[cfg]),
                                   str(plan_path), str(out)], cfg=cfg, exe="python", timeout=300)
    if r.rc != 0 or not out.exists():
        return None, ref
    return json.loads(out.read_text(encoding="utf-8")), ref


def _entries(call: dict | None, key: str):
    if not call or call.get("skipped") or call.get("client_error") or call.get("is_error"):
        return None
    s = call.get("structured")
    if not isinstance(s, dict):
        return None
    v = s.get(key)
    return v if isinstance(v, list) else None


#: The promises, each with the exact sentence it rests on and where that sentence lives.
#: (row id, audit, source, sentence, call label, entries key, slice start, spec)
CONTRACT = [
    # --- "confidence-tagged": a node reached through an edge carries the edge's confidence
    *[(f"C1.confidence.{lab}", "W03", "instructions", "confidence-tagged", lab, key, start,
       {"min": mn, "fields": {"confidence": "conf"}})
      for lab, key, start, mn in (
          ("find_callers.compute_tax", "nodes", 0, 3),
          ("find_callers.createClient", "nodes", 0, 1),
          ("find_callers.PostEntry", "nodes", 0, 1),
          ("find_callees.issue_invoice", "nodes", 0, 2),
          ("find_dependents.@acme/api-client", "nodes", 0, 1),
          ("shortest_path.renderOrders->createClient", "nodes", 1, 1),
          ("blast_radius.compute_tax", "hits", 0, 2),
          ("get_neighbors.compute_tax", "edges", 0, 1))],
    ("C1.confidence.ask.callers", "W03", "ask", "Graph routes are cited and confidence-tagged",
     "ask.callers", "nodes", 0, {"min": 2, "fields": {"confidence": "conf"}}),
    # --- 9.7.0's refinement: a lookup or a search node carries no confidence
    *[(f"C2.no_confidence_on_lookup.{lab}", "", "instructions",
       "a node from a lookup or a search (`hybrid_search` included) has none", lab, key, 0,
       {"min": 1, "fields": {"confidence": "null"}})
      for lab, key in (("search_code.InvoiceService", "nodes"),
                       ("find_definition.compute_tax", "nodes"))],
    # --- every cited node carries citation_status (fresh index: expect `verified`)
    *[(f"C3.citation_status.{lab}", "", "instructions",
       "Every cited node carries citation_status", lab, key, start,
       {"min": 1, "when_file": True, "fields": {"citation_status": "verified"}})
      for lab, key, start in (
          ("search_code.InvoiceService", "nodes", 0), ("find_definition.compute_tax", "nodes", 0),
          ("get_node.InvoiceService", "@self", 0), ("find_callers.compute_tax", "nodes", 0),
          ("find_callees.issue_invoice", "nodes", 0),
          ("find_dependents.@acme/api-client", "nodes", 0),
          ("shortest_path.renderOrders->createClient", "nodes", 0),
          ("blast_radius.compute_tax", "hits", 0), ("ask.callers", "nodes", 0))],
    # --- "cited (source file + verified date)"
    *[(f"C4.cited_with_date.{lab}", "", "instructions",
       "Results are cited (source file + verified date)", lab, key, start,
       {"min": 1, "fields": {fkey: "nonnull", "verified_at": "nonnull"}})
      for lab, key, start, fkey in (
          ("get_neighbors.compute_tax", "edges", 0, "source_file"),
          ("find_callers.compute_tax", "nodes", 0, "file"),
          ("find_definition.compute_tax", "nodes", 0, "file"),
          ("blast_radius.compute_tax", "hits", 0, "file"))],
    # --- tool descriptions
    ("C5.call_site.find_callers", "", "find_callers",
     "Each entry carries `call_file`/`call_line`", "find_callers.compute_tax", "nodes", 0,
     {"min": 3, "fields": {"call_file": "nonnull", "call_line": "int>=1"}}),
    ("C5.call_site.find_callees", "", "find_callees",
     "each carrying the `call_file`/`call_line` the call is written on",
     "find_callees.issue_invoice", "nodes", 0,
     {"min": 2, "fields": {"call_file": "nonnull", "call_line": "int>=1"}}),
    ("C7.edge_site.find_dependents", "", "find_dependents",
     "Each entry carries the `edge_file`/`edge_line` of the manifest",
     "find_dependents.@acme/api-client", "nodes", 0,
     {"min": 1, "fields": {"edge_file": "nonnull", "edge_line": "int>=1"}}),
    ("C8.edge_site.shortest_path", "", "shortest_path",
     "Each node after the first carries the `edge_file`/`edge_line` and `confidence`",
     "shortest_path.renderOrders->createClient", "nodes", 1,
     {"min": 1, "fields": {"edge_file": "nonnull", "edge_line": "int>=1"}}),
    ("C9.hit_fields.blast_radius", "", "blast_radius",
     "Each hit carries its hop distance, the relation, and confidence",
     "blast_radius.compute_tax", "hits", 0,
     {"min": 2, "fields": {"hop": "int>=1", "via": "nonnull", "confidence": "conf"}}),
    ("C16.repo_fields.list_repos", "", "list_repos",
     "Each entry carries the branch, indexed head, and last-index time", "list_repos", "repos", 0,
     {"min": 4, "fields": {"default_branch": "nonnull", "head_commit": "nonnull",
                           "indexed_at": "nonnull", "node_count": "int>=1"}}),
]

#: Single-object promises: (row id, source, sentence, call label, spec on the result object)
CONTRACT_OBJ = [
    ("C10.note.search_code", "search_code", "a `note`", "search_code.filter_excludes_all",
     {"note": "nonnull"}),
    ("C11.note.find_definition", "find_definition", "Empty carries a `note`",
     "find_definition.missing", {"note": "nonnull"}),
    ("C12.note.find_dependents", "find_dependents", "An unknown package returns `note`",
     "find_dependents.missing", {"note": "nonnull"}),
    ("C6.note.find_callers", "find_callers",
     "`note` reports the distinct-caller count whenever it differs from the entry count",
     "find_callers.compute_tax", {"note": "nonnull"}),
    ("C9.note.blast_radius", "blast_radius", "An unresolvable symbol returns `note`",
     "blast_radius.missing", {"note": "nonnull"}),
    ("C8.gap.shortest_path", "shortest_path", "`gap` says which of", "shortest_path.missing",
     {"gap": "nonnull"}),
    ("C17.indexed.graph_health", "graph_health", "Read ``indexed`` before the counts",
     "graph_health", {"indexed": "nonnull"}),
    ("C19.readme.get_readme", "get_readme", "Returns the first", "get_readme.billing-core",
     {"found": "nonnull", "path": "nonnull"}),
    ("C18.doc.get_generated_doc", "get_generated_doc", "``kind`` is ``\"api\"``",
     "get_generated_doc.api", {"found": "nonnull", "markdown": "nonnull"}),
]

#: found=False promises for an unknown repo: (row id, source, call label)
CONTRACT_FOUND_FALSE = [
    ("C13.found_false.repo_dependencies", "repo_dependencies", "repo_dependencies.missing"),
    ("C14.found_false.who_knows", "who_knows", "who_knows.missing"),
    ("C15.found_false.get_repo_links", "get_repo_links", "get_repo_links.missing"),
]


#: Fields the named-value rows in mcp_oracles check (C6, C10, C13-C15, C17, C18, C20).
NAMED_VALUE_FIELDS = {"found", "score", "note", "total", "truncated", "indexed", "repos",
                      "stale", "dangling", "empty", "shard", "unreadable", "doc_commit",
                      "current_commit"}


def _promise_text(rec: dict, source: str) -> str:
    """The instructions or one tool's description, whitespace-collapsed for matching."""
    if source == "instructions":
        text = rec.get("instructions") or ""
    else:
        text = next((t.get("description") or "" for t in rec.get("tools", [])
                     if t["name"] == source), "")
    return " ".join(text.split())


def _has(rec: dict, source: str, sentence: str) -> bool:
    return " ".join(sentence.split()) in _promise_text(rec, source)


def mcp_oracles(ctx: Ctx, X: dict) -> None:
    R = X["repos"]
    bc, wa, lg = R["billing-core"], R["web-app"], R["ledger"]
    rec, ref = run_mcp(ctx, "main", _plan(X))
    if rec is None:
        judge("C0.mcp_session", "the MCP client completes a session", audit="W03",
              expected="session", actual=None, check=lambda a, e: ["MCP session failed"],
              broken=None, evidence=[ref])
        return
    calls = rec["calls"]
    ctx.mcp = rec
    for lab, c in calls.items():
        if c.get("client_error") or c.get("skipped"):
            print(f"  note: {lab}: {c.get('client_error') or c.get('skipped')}")

    def entries(lab, key, start):
        if key == "@self":
            c = calls.get(lab) or {}
            s = c.get("structured") if not c.get("is_error") else None
            # A tool returning a bare model arrives wrapped as {"result": {...}}.
            if isinstance(s, dict) and set(s) == {"result"}:
                s = s["result"]
            return [s] if isinstance(s, dict) else None
        v = _entries(calls.get(lab), key)
        return None if v is None else v[start:]

    for oid, audit, source, sentence, lab, key, start, spec in CONTRACT:
        if not _has(rec, source, sentence):
            na(oid, f"{source}: '{sentence}'", audit=audit, evidence=[ref],
               reason=f"promise text not found in this server's {source}")
            continue
        judge(oid, f"{lab}: {source} promises '{sentence}'", audit=audit, expected=spec,
              actual=entries(lab, key, start), check=ck_entries, broken=broken_spec(spec),
              evidence=[ref])
    for oid, source, sentence, lab, spec in CONTRACT_OBJ:
        if not _has(rec, source, sentence):
            na(oid, f"{source}: '{sentence}'", audit="", evidence=[ref],
               reason=f"promise text not found in this server's {source} description")
            continue
        c = calls.get(lab) or {}
        obj = c.get("structured") if isinstance(c.get("structured"), dict) else None
        judge(oid, f"{lab}: {source} promises '{sentence}'", audit="", expected=spec,
              actual=obj, check=ck_field, broken=broken_spec(spec), evidence=[ref])
    for oid, _source, lab in CONTRACT_FOUND_FALSE:
        c = calls.get(lab) or {}
        obj = c.get("structured") if isinstance(c.get("structured"), dict) else None
        judge(oid, f"{lab}: ``found=False`` for an unknown repo", audit="",
              expected=False, actual=None if obj is None else obj.get("found"), check=ck_eq,
              broken=True, evidence=[ref])

    # Named values carried by the contract fields.
    callers = _entries(calls.get("find_callers.compute_tax"), "nodes")
    judge("C5.call_lines.compute_tax", "find_callers compute_tax: the three call lines",
          audit="", expected=[("src/billing/invoice.py", 12), ("src/billing/invoice.py", 17),
                              ("src/billing/invoice.py", 18)],
          actual=None if callers is None else [(n.get("call_file"), n.get("call_line"))
                                               for n in callers],
          check=ck_set_eq, broken=[("src/billing/invoice.py", 99)], evidence=[ref])
    deps = _entries(calls.get("find_dependents.@acme/api-client"), "nodes")
    judge("C7.edge_line.@acme/api-client", "find_dependents: the manifest line declaring it",
          audit="", expected=[(wa, "packages/ui/package.json", 5)],
          actual=None if deps is None else [
              (n.get("repo"), n.get("edge_file"), n.get("edge_line")) for n in deps],
          check=ck_set_eq, broken=[(wa, "packages/ui/package.json", 6)], evidence=[ref])
    # PyPI names are one package across `-` and `_` (PEP 503) and share one node id. The
    # spelling billing-core's manifest uses must still find billing-core.
    deps = _entries(calls.get("find_dependents.acme-ledger-client"), "nodes")
    judge("O11.mcp.find_dependents.pypi_spelling",
          "find_dependents acme-ledger-client (billing-core's own spelling) lists billing-core",
          audit="new", expected=[(bc, "pyproject.toml", 5)],
          actual=None if deps is None else [
              (n.get("repo"), n.get("edge_file"), n.get("edge_line")) for n in deps],
          check=ck_superset, broken=[(bc, "pyproject.toml", 6)], evidence=[ref],
          note="ledger's manifest spells it acme_ledger_client; both normalise to one id")
    stats = (calls.get("graph_stats") or {}).get("structured") or {}
    judge("O1.mcp.graph_stats_repos", "graph_stats: repos == 4", audit="",
          expected=X["repo_count"], actual=stats.get("repos"), check=ck_eq,
          broken=X["repo_count"] + 1, evidence=[ref])
    repos = _entries(calls.get("list_repos"), "repos")
    judge("O1.mcp.list_repos_ids", "list_repos: the four fixture repo ids", audit="",
          expected=sorted(R.values()),
          actual=None if repos is None else [r.get("id") for r in repos],
          check=ck_set_eq, broken=sorted(R.values())[1:] + ["zz/repo"], evidence=[ref])
    # Repo -> repo dependencies, second surface for O11.
    for name, repo in (("billing-core", bc), ("web-app", wa), ("ledger", lg)):
        edges = _entries(calls.get(f"repo_dependencies.{name}"), "edges")
        judge(f"O11.mcp.repo_dependencies.{name}", f"repo_dependencies {name} out",
              audit="new" if name == "ledger" else "",
              expected=sorted(X["repo_deps_out"][repo]),
              actual=None if edges is None else [e.get("dst") for e in edges],
              check=ck_set_eq, broken=sorted(X["repo_deps_out"][repo]) + ["zz/repo"],
              evidence=[ref])
    # Named values behind fields the descriptions promise.
    obj = (calls.get("find_callers.compute_tax") or {}).get("structured") or {}
    judge("C6.note_value.find_callers",
          "find_callers compute_tax: `note` states 3 sites, 2 callers",
          audit="", expected=[str(X["compute_tax_sites"]), str(X["compute_tax_distinct_callers"])],
          actual=obj.get("note"), check=ck_mentions, broken=["17"], evidence=[ref])
    head = ctx.heads["ws"]["billing-core"]
    obj = (calls.get("get_generated_doc.api") or {}).get("structured")
    judge("C18.fresh.get_generated_doc.api", "get_generated_doc api: fresh, stamped with the head",
          audit="", expected={"found": True, "stale": False, "doc_commit": head,
                              "current_commit": head}, actual=obj, check=ck_subset,
          broken={"found": True, "stale": True, "doc_commit": head, "current_commit": head},
          evidence=[ref])
    obj = (calls.get("graph_health") or {}).get("structured")
    want = {"indexed": True, "repos": X["repo_count"], "stale": 0, "dangling": 0, "empty": 0,
            "shard": 0, "unreadable": 0}
    judge("C17.counts.graph_health", "graph_health on a fresh fixture index", audit="",
          expected=want, actual=obj, check=ck_subset, broken={**want, "repos": 99},
          evidence=[ref])
    obj = (calls.get("search_code.InvoiceService") or {}).get("structured") or {}
    nodes = obj.get("nodes") if isinstance(obj.get("nodes"), list) else None
    judge("C10.total.search_code", "search_code: `total` and `truncated` agree with the list",
          audit="", expected={"total": None if nodes is None else len(nodes), "truncated": False},
          actual=obj, check=ck_subset, broken={"total": -1, "truncated": False}, evidence=[ref])
    # Leading JSDoc reaches the node's `doc` (exported and module-private).
    for n, f, doc in X["jsdoc"]:
        nodes = _entries(calls.get(f"find_definition.doc.{n}"), "nodes")
        judge(f"O12.jsdoc.{n}", f"find_definition {n}: `doc` holds its JSDoc", audit="new",
              expected=[{"name": n, "file": f, "doc": doc}], actual=nodes, check=ck_contains,
              broken=[{"name": n, "file": f, "doc": doc + " (x)"}], evidence=[ref])
    # W01 second surface.
    nodes = _entries(calls.get("find_definition.orders_table"), "nodes")
    judge("O7.mcp.find_definition.orders", "find_definition orders kind=table", audit="W01",
          expected=[{"name": "orders", "file": "db/01_if_not_exists.sql", "kind": "table"}],
          actual=nodes, check=ck_contains,
          broken=[{"name": "orders", "file": "db/zz.sql", "kind": "table"}], evidence=[ref])
    # D05 second surface.
    for label, _repo, a, b, _t in X["collisions"]:
        nodes = _entries(calls.get(f"search_code.file.{label}"), "nodes")
        judge(f"O8.mcp.file_node.{label}", f"search_code kind=file: {a} and {b}", audit="D05",
              expected=[a, b], actual=None if nodes is None else [n.get("file") for n in nodes],
              check=ck_superset, broken=[a, b, a + ".zz"], evidence=[ref],
              portable=(label != "ts-case-only"))
    _promise_inventory(rec)

    # Vector tools: a second session on the embeddings store.
    erec, eref = run_mcp(ctx, "emb", _emb_plan())
    if erec is None:
        judge("C0.mcp_session_emb", "the MCP client completes a session on the vector store",
              audit="", expected="session", actual=None, check=lambda a, e: ["session failed"],
              broken=None, evidence=[eref])
        return
    ctx.mcp_emb = erec
    for tool in ("semantic_search", "hybrid_search"):
        c = erec["calls"].get(tool) or {}
        hits = _entries(c, "nodes")
        if hits is None and isinstance(c.get("structured"), dict):
            hits = next((v for v in c["structured"].values() if isinstance(v, list)), None)
        spec = {"min": 3, "fields": {"score": "number"}}
        judge(f"C20.score.{tool}", f"{tool}: every hit carries a similarity `score`", audit="",
              expected=spec, actual=hits, check=ck_entries, broken=broken_spec(spec),
              evidence=[eref], note=("instructions: `hybrid_search` nodes carry no confidence"
                                     if tool == "hybrid_search" else ""))
        spec = {"min": 3, "when_file": True, "fields": {"citation_status": "verified"}}
        judge(f"C3.citation_status.{tool}", f"{tool}: every cited node carries citation_status",
              audit="", expected=spec, actual=hits, check=ck_entries, broken=broken_spec(spec),
              evidence=[eref])
        if _has(erec, "instructions",
                "a node from a lookup or a search (`hybrid_search` included) has none"):
            spec = {"min": 3, "fields": {"confidence": "null"}}
            judge(f"C2.no_confidence_on_lookup.{tool}", f"{tool}: no confidence on search hits",
                  audit="", expected=spec, actual=hits, check=ck_entries,
                  broken=broken_spec(spec), evidence=[eref])


def _promise_inventory(rec: dict) -> None:
    """Every backticked name in a tool description that is also an output-schema field.

    Lists them beside the rows that check them, so a promise no row reads is visible rather
    than silently unchecked. Not an oracle: it judges nothing.
    """
    checked = {f for _o, _a, _s, _sen, _l, _k, _st, spec in CONTRACT for f in spec["fields"]}
    checked |= {f for *_x, spec in CONTRACT_OBJ for f in spec} | NAMED_VALUE_FIELDS
    inv = {}
    for t in rec.get("tools", []):
        schema = json.dumps(t.get("output_schema") or {})
        named = set(re.findall(r"``?([a-z_]+)``?", t.get("description") or ""))
        fields = sorted(n for n in named if f'"{n}"' in schema)
        if fields:
            inv[t["name"]] = {"promised": fields,
                              "unchecked": [f for f in fields if f not in checked]}
    ROWS.append({"oid": "INV.promised_fields", "title": "backticked output fields per tool",
                 "audit": "", "portable": True, "status": "INFO", "fired": [], "n_fired": 0,
                 "harness_error": None, "breaktest": "n/a", "expected": "", "actual": inv,
                 "evidence": [], "note": "not an oracle"})


# ======================================================================================
# Main
# ======================================================================================
def main(argv: list[str] | None = None, *, run=None, make_target=None,
         root: Path | None = None, raw: Path | None = None) -> int:
    """Run tier E once. A failing oracle is a result, so the exit code is 0; a crash is not.

    A private wrapper passes ``run``, ``make_target`` (target name -> target), ``root``
    and ``raw``. Without ``run`` this is the CI path: ``ci_runner`` builds all four from
    ``--venv``, ``--root`` and ``--out``, and refuses to run outside GitHub Actions.
    """
    ap = argparse.ArgumentParser(description="tier E output oracles")
    ap.add_argument("--target", required=True, help="the contextlake version under test")
    ap.add_argument("--portable", action="store_true",
                    help="build the fixture with --portable (the tier F subset)")
    ap.add_argument("--tag", help="run directory name under the roots "
                                  "(default: <target>, plus -portable with --portable)")
    ap.add_argument("--hf-cache", help="Hugging Face hub cache that holds potion-base-8M "
                                       "(default: ~/.cache/huggingface/hub)")
    ci = ap.add_argument_group("CI path only (a wrapper that passes its own runner ignores these)")
    ci.add_argument("--venv", help="the venv contextlake is installed in")
    ci.add_argument("--root", help="short working root, for example $RUNNER_TEMP/f")
    ci.add_argument("--out", help="results root: results.json and raw output go to <out>/<tag>/")
    a = ap.parse_args(argv)
    if run is None:
        import ci_runner
        ci_runner.require_ci()
        missing = [f"--{k}" for k in ("venv", "root", "out") if not getattr(a, k)]
        if missing:
            ap.error(f"the CI path needs {' '.join(missing)}")
        target = ci_runner.Target.from_venv(Path(a.venv), name=a.target)
        run, root, raw = ci_runner.run, ci_runner.tier_root(Path(a.root)), Path(a.out)
    else:
        try:
            target = make_target(a.target)
        except KeyError:
            ap.error(f"unknown target {a.target!r}")
    t0 = time.monotonic()
    ctx = Ctx(run, target, root, raw, a.portable, tag=a.tag,
              hf_cache=Path(a.hf_cache) if a.hf_cache else None)
    X = fixture_e.expected(a.portable)
    print(f"tier E: target {a.target} portable={a.portable} root={ctx.root}")
    print(f"  platform {sys.platform} ({platform.platform()}), harness python "
          f"{platform.python_version()}")
    print(f"  fixture_e {fixture_e.__file__}")
    print(f"  mcp client {HERE / 'mcp_client_e.py'}")
    print(f"  D05 pairs in this fixture: {[c[0] for c in X['collisions']]} "
          f"plus cross_repo {X['cross_repo_collision'][1]}")
    print(f"  model source {ctx.hf_cache / MODEL}")
    heads = setup(ctx)
    ctx.heads = heads
    print(f"  fixture {X['fixture_version']} git date {X['git_date']} heads {heads['ws']}")
    cli_oracles(ctx, X)
    mcp_oracles(ctx, X)
    summary = {
        "target": a.target, "portable": a.portable, "fixture_version": X["fixture_version"],
        "tag": ctx.tag, "platform": sys.platform, "os": platform.platform(),
        "harness_python": platform.python_version(),
        "git_date": X["git_date"], "heads": heads, "seconds": round(time.monotonic() - t0, 1),
        "counts": {s: sum(1 for r in ROWS if r["status"] == s)
                   for s in ("PASS", "FAIL", "ERROR", "N/A", "INFO")},
        "blind_oracles": [r["oid"] for r in ROWS if r["status"] in ("PASS", "FAIL")
                          and not r["breaktest"].startswith("fired")],
        "audit_hits": {aid: [r["oid"] for r in ROWS
                             if r["status"] == "FAIL" and r["audit"] == aid]
                       for aid in ("W01", "D05", "W03", "D05-lint", "W01-consequence", "new")},
        "rows": ROWS, "calls": ctx.calls,
    }
    targeted = {"W01", "W03", "W01-consequence"}
    summary["findings"] = {
        "known_open_D05": [r["oid"] for r in ROWS if r["status"] == "FAIL"
                           and r["audit"] in ("D05", "D05-lint")],
        "audit_ids_still_open": [r["oid"] for r in ROWS if r["status"] == "FAIL"
                                 and r["audit"] in targeted],
        "other": [r["oid"] for r in ROWS if r["status"] == "FAIL"
                  and r["audit"] not in targeted | {"D05", "D05-lint"}],
    }
    out = ctx.raw / "results.json"
    out.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(f"\n{summary['counts']}  blind={summary['blind_oracles']}")
    for aid, oids in summary["audit_hits"].items():
        print(f"  {aid:16} {len(oids)} hit(s): {oids[:6]}")
    for k, oids in summary["findings"].items():
        print(f"  findings {k}: {oids}")
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
