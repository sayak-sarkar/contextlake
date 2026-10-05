"""Compare two tier E summaries (Linux and Windows) oracle by oracle.

    python scripts/stability/diff_e.py LINUX_RESULTS_JSON WINDOWS_RESULTS_JSON

Prints the run headers side by side (fixture version, commit heads, counts, blind oracles),
then one line per oracle id: the id, the verdict in each file, and a mark:

- ``VERDICT`` when the verdicts differ. Both actual values and both first failure lines follow.
- ``VALUES`` when the verdicts match but the actual values differ. Both actual values follow.
- ``ONLY`` when the id is in one file only.

Before values are compared, timestamps (``*_at`` fields) are masked and the members of each
stored ``frozenset({...})`` are sorted: both differ between two runs that agree. ``actual``
is stored cut to 600 characters, so a difference past that point does not show.

Exit 0 when both files were read, whatever the verdicts; exit 2 when one could not be read
or holds no rows.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_TIME = re.compile(r'("[a-z_]*_at"\s*:\s*)"[^"]*"')
_FROZENSET = re.compile(r"frozenset\(\{([^{}]*)\}\)")


def _sorted_frozenset(m: re.Match) -> str:
    """A frozenset is stored as its str(), whose member order follows the per-process hash
    seed. Sort the members, so two runs that hold the same set print the same text."""
    try:
        members = ast.literal_eval("[" + m.group(1) + "]")
    except (ValueError, SyntaxError):
        return m.group(0)
    return "frozenset({" + ", ".join(sorted(repr(x) for x in members)) + "})"


def _mask(actual) -> str:
    text = actual if isinstance(actual, str) else json.dumps(actual, sort_keys=True)
    return _FROZENSET.sub(_sorted_frozenset, _TIME.sub(r'\1"<time>"', text))


def _load(path: str) -> dict:
    try:
        summary = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"diff_e: cannot read {path}: {e}") from e
    if not summary.get("rows"):
        raise SystemExit(f"diff_e: {path} holds no oracle rows")
    return summary


def _keyed(rows: list[dict]) -> dict[tuple[str, int], dict]:
    """Rows by (oid, occurrence), so a repeated id cannot hide a row."""
    seen: dict[str, int] = {}
    out = {}
    for r in rows:
        n = seen.get(r["oid"], 0)
        seen[r["oid"]] = n + 1
        out[(r["oid"], n)] = r
    return out


def _label(summary: dict, fallback: str) -> str:
    return {"linux": "linux", "win32": "windows", "darwin": "macos"}.get(
        summary.get("platform", ""), fallback)


def diff(a: dict, b: dict, out=sys.stdout) -> dict[str, int]:
    la, lb = _label(a, "A"), _label(b, "B")
    if la == lb:
        la, lb = la + "-A", lb + "-B"
    w = max(len(la), len(lb), 7)

    def p(line: str = "") -> None:
        print(line, file=out)

    p(f"tier E diff: {la} = {a.get('os', '?')}, {lb} = {b.get('os', '?')}")
    for key in ("target", "portable", "fixture_version", "git_date"):
        va, vb = a.get(key), b.get(key)
        p(f"  {key:16} {'same' if va == vb else 'DIFF'}: {va!r} | {vb!r}")
    for ws in sorted(set(a.get("heads", {})) | set(b.get("heads", {}))):
        ha, hb = a.get("heads", {}).get(ws), b.get("heads", {}).get(ws)
        p(f"  heads {ws:10} {'same' if ha == hb else 'DIFF'}: {ha} | {hb}")
    p(f"  counts {la}: {a.get('counts')}")
    p(f"  counts {lb}: {b.get('counts')}")
    p(f"  blind  {la}: {a.get('blind_oracles')}")
    p(f"  blind  {lb}: {b.get('blind_oracles')}")
    p()
    ra, rb = _keyed(a["rows"]), _keyed(b["rows"])
    order = list(ra) + [k for k in rb if k not in ra]
    width = max(len(oid) for oid, _n in order)
    p(f"{'oracle':{width}}  {la:{w}}  {lb:{w}}")
    tally = {"same": 0, "VERDICT": 0, "VALUES": 0, "ONLY": 0}
    for key in order:
        x, y = ra.get(key), rb.get(key)
        sa = x["status"] if x else "-"
        sb = y["status"] if y else "-"
        if x is None or y is None:
            mark = "ONLY"
        elif sa != sb:
            mark = "VERDICT"
        elif _mask(x.get("actual")) != _mask(y.get("actual")):
            mark = "VALUES"
        else:
            mark = "same"
        tally[mark] += 1
        oid = key[0] + (f" (#{key[1] + 1})" if key[1] else "")
        p(f"{oid:{width}}  {sa:{w}}  {sb:{w}}  {'' if mark == 'same' else mark}".rstrip())
        if mark in ("VERDICT", "VALUES"):
            for lab, r in ((la, x), (lb, y)):
                p(f"    {lab:{w}} actual: {_mask(r.get('actual'))}")
            if mark == "VERDICT":
                for lab, r in ((la, x), (lb, y)):
                    first = (r.get("fired") or [r.get("harness_error") or "(nothing fired)"])[0]
                    p(f"    {lab:{w}} fired:  {first}")
    p()
    p(f"{len(order)} oracle ids: {tally['same']} same, {tally['VERDICT']} with a different "
      f"verdict, {tally['VALUES']} with the same verdict and different values, "
      f"{tally['ONLY']} in one file only")
    return tally


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="per-oracle diff of two tier E summaries")
    ap.add_argument("first", help="results.json from the Linux cell")
    ap.add_argument("second", help="results.json from the Windows cell")
    a = ap.parse_args(argv)
    try:
        first, second = _load(a.first), _load(a.second)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    diff(first, second)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
