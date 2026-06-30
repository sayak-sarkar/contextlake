"""Static, offline ``--site`` export of the dashboard.

Materializes a self-contained folder: the SPA shell (``index.html``), its assets, and
the snapshot emitted as a classic-script GLOBAL (``data.js`` -> ``window.__CONTEXTLAKE__``)
loaded before the SPA — so the dashboard renders from ``file://`` with zero ``fetch``
(blocked under ``file://``). A ``data.json`` copy is kept for inspection. The iframed
cytoscape graph pages are produced by ``visualize.build_site`` (already offline).

PII GUARDRAIL (binding — see the plan): a snapshot from a **real** store inlines real
repo ids, git-author identities and connector URLs, none of which ``sanitize_label``
scrubs. Therefore:

* ``sample=True`` builds the snapshot from the committed ``examples/fixtures/
  sample-graph.json`` so the source-genericity guard applies — this is the only safe
  public showcase build;
* a real-store build WITHOUT ``--anonymize`` still works locally but prints a loud
  "do not publish unscrubbed" warning;
* ``anonymize=True`` hashes author identities and strips external link URLs.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ...logging_setup import log
from ..security import sanitize_label
from ..store.sqlite_store import SqliteStore
from . import data as kbdata

# Symbol kinds worth a reverse-impact precompute (sources, not leaf files/pages).
_IMPACT_KINDS = {"class", "function", "method", "interface", "struct", "enum",
                 "module", "package", "endpoint", "topic"}

_PUBLISH_WARNING = (
    "WARNING: this --site snapshot was built from a REAL store. Do NOT publish it "
    "unscrubbed — it contains repo names + git author identities + connector URLs. "
    "Use --anonymize for a shareable build, or --sample for the public showcase."
)

_PUBLISH_WARNING_ANON = (
    "WARNING: this --site snapshot was built from a REAL store with --anonymize. "
    "Author identities are hashed, link URLs and README/wiki prose are omitted, but "
    "repo names + structural facts remain — review before publishing. Use --sample "
    "for a guaranteed-generic public showcase."
)

# A static export caps the heavy per-repo detail to a representative slice (spec §10):
# repo_detail computes git-history owners per repo, so a 480-repo export would be both
# slow to build and huge. Repos beyond the cap appear in the fleet overview but have no
# detail panel — the dashboard guides those clicks to the live server. ``--repos``
# overrides the cap with an explicit selection. Prefer repos with content for the slice.
_DETAIL_CAP = 60


def _static(name: str) -> str:
    from importlib.resources import files
    return (files("contextlake.kb.dashboard") / "static" / name).read_text(encoding="utf-8")


def _fixture_path() -> Path:
    # .../src/contextlake/kb/dashboard/site.py -> repo root is parents[4]. A multi-repo
    # bundle ({"shards": [...]}) so the showcase reads like a real fleet; sample-graph.json
    # stays a single shard for `index --source` demos.
    return Path(__file__).resolve().parents[4] / "examples" / "fixtures" / "sample-dashboard.json"


def _sample_store(tmp: Path):
    """An ephemeral store loaded from the committed sample fixture (no real PII)."""
    from ..model import Repo
    from ..store.shards import GraphShard, reindex_shard, write_shard

    fixture = _fixture_path()
    if not fixture.exists():
        raise FileNotFoundError(
            f"sample fixture not found at {fixture} — --sample builds from the "
            "committed examples/fixtures/sample-graph.json (run from a source checkout)")
    import json

    raw = json.loads(fixture.read_text(encoding="utf-8"))
    # The fixture is a bundle of shards ({"shards": [...]}) for a multi-repo showcase;
    # a bare single shard is still accepted for back-compat.
    shard_dicts = raw["shards"] if isinstance(raw, dict) and "shards" in raw else [raw]
    shards = [GraphShard.model_validate(s) for s in shard_dicts]
    store = SqliteStore(tmp / "index.sqlite")
    for shard in shards:
        # path -> the (README-less) tmp dir, so owners/readme resolve to empty, never cwd.
        store.upsert_repo(Repo(id=shard.repo, path=str(tmp), head_commit=shard.head_commit))
        write_shard(tmp, shard)
        reindex_shard(store, tmp, shard.repo)
        store.mark_indexed(shard.repo, shard.head_commit, "2026-01-01T00:00:00Z")
    return store


def _patterns(repos) -> list[str] | None:
    if not repos:
        return None
    if isinstance(repos, str):
        return [p.strip() for p in repos.split(",") if p.strip()] or None
    return [p for p in repos if p] or None


def _symbol_index(store, patterns, *, cap: int = 2000) -> list[dict]:
    """A flat, bounded symbol search index (id + display fields), so static Symbols
    search and Blast-Radius seeding work offline. Bounded per spec §10 ("representative
    slice") — a real 480-repo export would otherwise explode. ``_raw`` is the unsanitized
    node id, kept only for the impact precompute and stripped before serialization."""
    from ..visualize import _match_repo

    out: list[dict] = []
    for r in store.conn.execute(
            "SELECT node_id, repo_id, kind, name, qualified_name, file, line_start, lang "
            "FROM nodes WHERE name IS NOT NULL ORDER BY repo_id, name"):
        rid = r["repo_id"]
        if patterns and not _match_repo(rid, patterns):
            continue
        out.append({
            "id": sanitize_label(r["node_id"]),
            "repo": sanitize_label(rid),
            "kind": sanitize_label(r["kind"]),
            "name": sanitize_label(r["name"]),
            "qualified_name": sanitize_label(r["qualified_name"]) if r["qualified_name"] else None,
            "file": sanitize_label(r["file"]) if r["file"] else None,
            "line": r["line_start"],
            "lang": sanitize_label(r["lang"]) if r["lang"] else None,
            "_raw": r["node_id"],
        })
        if len(out) >= cap:
            break
    return out


def _impact_index(store, symbols: list[dict], *, cap: int = 400) -> dict:
    """Reverse blast-radius precompute keyed by (sanitized) node id, for the offline
    Blast-Radius panel. Computed at the widest setting so the client narrows by
    filtering hops/relations; bounded to the symbol-index set."""
    out: dict[str, dict] = {}
    for s in symbols:
        if len(out) >= cap:
            break
        if s["kind"] not in _IMPACT_KINDS:
            continue
        imp = kbdata.impact(store, s["_raw"], hops=3, limit=100)
        if imp.get("found") and imp.get("hits"):
            out[s["id"]] = imp
    return out


def _snapshot(store, store_dir: Path, *, repos=None, anonymize: bool = False,
              group_depth: int = 1) -> dict:
    """Build the full JSON-able dashboard snapshot from the data functions."""
    from ..visualize import _BRAND, CONF_META, _match_repo

    overview = kbdata.fleet_overview(store, group_depth=group_depth)
    patterns = _patterns(repos)
    repo_ids = [r["id"] for r in overview["repos"]]
    if patterns:
        repo_ids = [r for r in repo_ids if _match_repo(r, patterns)]
    # A static export carries detail for a representative slice — repo_detail computes
    # git-history owners per repo, so building hundreds is slow; the live server renders
    # detail per-click instead (the dashboard guides there for repos beyond the slice).
    # ``--repos`` overrides the cap with an explicit selection; prefer repos with content
    # so the slice isn't spent on empty repos.
    if patterns:
        detail_ids = repo_ids
    else:
        node_counts = {r["id"]: (r.get("node_count") or 0) for r in overview["repos"]}
        with_content = [rid for rid in repo_ids if node_counts.get(rid)] or repo_ids
        detail_ids = with_content[:_DETAIL_CAP]
    details = {rid: kbdata.repo_detail(store, store_dir, rid, anonymize=anonymize)
               for rid in detail_ids}
    # One bucketed edge scan for all repos, not one full scan per repo (O(repos x edges)).
    relationships = kbdata.repo_relationships_bulk(store, detail_ids)
    symbols = _symbol_index(store, patterns)
    impact = _impact_index(store, symbols)
    for s in symbols:
        s.pop("_raw", None)
    return {
        "mode": "static",
        "snapshot_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "confidence": CONF_META,
        "brand": _BRAND,
        "overview": overview,
        "repos": details,
        "relationships": relationships,
        "health": kbdata.health(store, store_dir),
        "symbols": symbols,
        "impact": impact,
        "anonymized": anonymize,
    }


def _emit(out: Path, store, store_dir: Path, snapshot: dict, repos) -> None:
    """Write the snapshot global (data.js) + data.json, the SPA shell + assets, the graph site."""
    from .. import visualize as viz

    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (out / "dashboard.js").write_text(_static("dashboard.js"), encoding="utf-8")
    (out / "dashboard.css").write_text(_static("dashboard.css"), encoding="utf-8")

    # Emit the snapshot as a classic-script GLOBAL assignment (file://-safe — fetch()
    # is blocked under file://). The SPA prefers ``window.__CONTEXTLAKE__`` when present
    # and falls back to live ``fetch`` otherwise. ``</`` is escaped so the payload can
    # never break out of the <script> element. Kept as a SEPARATE ``data.js`` (not
    # inlined into index.html) so the SPA shell stays a tiny, fixed artifact and the live
    # server (which serves no data.js) stays a clean live-fetch path. Anonymized exports
    # drop README/wiki prose + link URLs at the source (kbdata.repo_detail), so data.js
    # carries no ``https://`` anchors either — the offline-boundary test scans it too.
    payload = json.dumps(snapshot).replace("</", "<\\/")
    (out / "data.js").write_text("window.__CONTEXTLAKE__ = " + payload + ";\n",
                                 encoding="utf-8")

    # Load the snapshot global BEFORE the SPA. Done by injection (not in the shared
    # dashboard.html) so the live server, which serves no data.js, stays a clean 404-free
    # live-fetch path.
    shell = _static("dashboard.html").replace(
        '<script src="dashboard.js"></script>',
        '<script src="data.js"></script>\n<script src="dashboard.js"></script>')
    (out / "index.html").write_text(shell, encoding="utf-8")

    # The architecture pages reuse the existing offline cytoscape site (cdn=False,
    # sibling assets, no live server) — iframed by the SPA.
    viz.build_site(store, out / "graph", repos=_patterns(repos), log=log)


def build_dashboard_site(store_dir, out_dir, *, repos=None, anonymize: bool = False,
                         sample: bool = False, group_depth: int = 1) -> Path:
    """Build a static, offline dashboard folder at ``out_dir``.

    With ``sample=True`` the snapshot is built from the committed sample fixture (the
    only safe public-showcase build). Otherwise it is built from the real store at
    ``store_dir``; ``anonymize=True`` scrubs author identities + external URLs, and a
    non-anonymized real-store build prints a loud do-not-publish warning.
    """
    out = Path(out_dir)
    if sample:
        tmp = Path(tempfile.mkdtemp(prefix="contextlake-dash-sample-"))
        store = _sample_store(tmp)
        try:
            snapshot = _snapshot(store, tmp, repos=repos, anonymize=anonymize,
                                 group_depth=group_depth)
            _emit(out, store, tmp, snapshot, repos)
        finally:
            store.close()
            shutil.rmtree(tmp, ignore_errors=True)
        return out

    store_dir = Path(store_dir)
    # Every real-store build prints a do-not-publish warning — anonymized ones too, since
    # repo names + structural facts survive. Only --sample (the generic fixture) is silent.
    log(_PUBLISH_WARNING_ANON if anonymize else _PUBLISH_WARNING)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        snapshot = _snapshot(store, store_dir, repos=repos, anonymize=anonymize,
                             group_depth=group_depth)
        _emit(out, store, store_dir, snapshot, repos)
    finally:
        store.close()
    return out
