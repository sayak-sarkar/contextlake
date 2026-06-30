"""Static offline `--site` export of the dashboard (kb/dashboard/site.py).

Builds the public showcase from the committed sample fixture (sample=True, no real
PII) and asserts the offline boundary + a well-formed SPA shell via stdlib parsing.
"""

import json
from html.parser import HTMLParser

from contextlake.kb.dashboard.site import build_dashboard_site
from contextlake.kb.ids import make_id
from contextlake.kb.model import Node, Repo
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

# The six panel root containers the SPA router shows/hides — present in the static
# shell so the dashboard is well-formed even before any JS runs.
_PANEL_IDS = {"panel-fleet", "panel-repo", "panel-arch", "panel-symbol",
              "panel-health", "panel-search"}


class _Shell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.html_lang = None
        self.h1_count = 0
        self.has_app = False
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.html_lang = a.get("lang")
        if tag == "h1":
            self.h1_count += 1
        if a.get("id"):
            self.ids.add(a["id"])
        if a.get("id") == "app":
            self.has_app = True


def test_sample_site_is_offline_and_well_formed(tmp_path):
    out = build_dashboard_site(tmp_path / "store", tmp_path / "site", sample=True)

    # data.json snapshot parses and has the expected top-level shape
    snap = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert set(snap) >= {"overview", "repos", "relationships", "health"}
    assert snap["overview"]["stats"]["repos"] >= 1
    assert "demo/app" in snap["repos"]
    # the offline data-delivery additions: snapshot mode/date + flat symbol index
    assert snap["mode"] == "static" and snap["snapshot_date"]
    assert isinstance(snap["symbols"], list) and snap["symbols"]
    assert "impact" in snap

    # the static export ships the snapshot as a classic-script GLOBAL (not fetch), and
    # that global parses back to the same payload the SPA reads at boot.
    data_js = (out / "data.js").read_text(encoding="utf-8")
    assert data_js.startswith("window.__CONTEXTLAKE__ = ")
    injected = json.loads(data_js[len("window.__CONTEXTLAKE__ = "):].rstrip().rstrip(";"))
    assert injected["overview"]["stats"]["repos"] == snap["overview"]["stats"]["repos"]

    # SPA shell: parseable, lang, exactly one <h1>, an #app mount, every panel container,
    # and the data.js global loaded before the SPA script.
    shell = (out / "index.html").read_text(encoding="utf-8")
    p = _Shell()
    p.feed(shell)
    assert p.html_lang == "en"
    assert p.h1_count == 1
    assert p.has_app
    assert _PANEL_IDS <= p.ids
    assert shell.index('src="data.js"') < shell.index('src="dashboard.js"')

    # offline boundary: the gate files reference no network/CDN resources. data.js is
    # scanned too — the injected snapshot must not smuggle external URLs into the export.
    for name in ("index.html", "dashboard.js", "dashboard.css", "data.js"):
        low = (out / name).read_text(encoding="utf-8").lower()
        assert "http://" not in low and "https://" not in low and "cdn." not in low, name

    # the iframed graph site was emitted alongside (reused visualize.build_site)
    assert (out / "graph" / "index.html").exists()
    assert (out / "dashboard.js").exists() and (out / "dashboard.css").exists()


def _real_store_with_readme(store_dir, readme, wiki=None):
    """A minimal real store at ``store_dir/index.sqlite`` whose one repo has a README
    (and optionally a wiki page) carrying prose — the PII surface --anonymize must scrub."""
    store_dir.mkdir(parents=True, exist_ok=True)
    clone = store_dir / "clone"
    clone.mkdir()
    (clone / "README.md").write_text(readme, encoding="utf-8")
    s = SqliteStore(store_dir / "index.sqlite")
    nodes = [
        Node(id=make_id("repo", "demo/app"), repo="demo/app", kind="repo", name="demo/app"),
        Node(id="demo_svc", repo="demo/app", kind="class", name="Svc",
             file="src/svc.py", lang="python"),
    ]
    s.upsert_repo(Repo(id="demo/app", path=str(clone), head_commit="h1"))
    write_shard(store_dir, GraphShard(repo="demo/app", head_commit="h1", nodes=nodes, edges=[]))
    reindex_shard(s, store_dir, "demo/app")
    s.mark_indexed("demo/app", "h1", "2026-06-01T00:00:00Z")
    if wiki is not None:
        (store_dir / "wiki").mkdir(exist_ok=True)
        (store_dir / "wiki" / "demo__app.md").write_text(wiki, encoding="utf-8")
    s.close()


def test_anonymize_build_drops_prose_and_external_urls(tmp_path):
    # A README + wiki with an author name AND an internal URL — exactly what would leak.
    _real_store_with_readme(
        tmp_path / "store",
        readme="# demo/app\n\nMaintained by Jane Doe — docs at https://internal.example.com/handbook\n",
        wiki="# demo/app\n\nWritten by Jane Doe. See https://internal.example.com/wiki for more.\n",
    )
    out = build_dashboard_site(tmp_path / "store", tmp_path / "site", anonymize=True)

    # the snapshot drops README/wiki prose (keeps the wiki flags)
    snap = json.loads((out / "data.json").read_text(encoding="utf-8"))
    detail = snap["repos"]["demo/app"]
    assert detail["readme_html"] is None
    assert detail["wiki"]["html"] is None
    assert detail["wiki"]["found"] is True

    # the shipped data.js carries no external URLs and no leaked author name
    data_js = (out / "data.js").read_text(encoding="utf-8").lower()
    assert "http://" not in data_js and "https://" not in data_js
    assert "jane doe" not in data_js


def test_group_depth_flows_into_the_snapshot(tmp_path):
    # depth=1 buckets repos by top-level namespace (acme, demo); depth=2 has no
    # 2nd-level namespace beyond the repo itself -> ungrouped
    out1 = build_dashboard_site(tmp_path / "s1", tmp_path / "d1", sample=True, group_depth=1)
    g1 = {g["group"] for g in json.loads((out1 / "data.json").read_text())["overview"]["groups"]}
    assert g1 == {"acme", "demo"}
    out2 = build_dashboard_site(tmp_path / "s2", tmp_path / "d2", sample=True, group_depth=2)
    g2 = {g["group"] for g in json.loads((out2 / "data.json").read_text())["overview"]["groups"]}
    assert g2 == {"(ungrouped)"}
