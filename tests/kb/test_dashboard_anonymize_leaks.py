"""`--anonymize` on the static export and on the served Chat tab.

Three leaks were found by reading every output file instead of the routes a test author
remembered:

* `--site --anonymize` dropped the wiki from `data.json` and `data.js`, and still wrote
  every wiki page in full to `graph/wiki-<slug>.html`. The older test scanned two files.
* `--serve --anonymize` answered a Chat question about a repo with the wiki text and the
  real author names, which `/api/repo/<id>/wiki` and the owners panel withhold.
* The route list in `test_dashboard_serve_anonymize.py` is written by hand, which is how
  the Chat route was missed. Here the list comes from the server's own source.

Every check that something is ABSENT sits next to a check that it is PRESENT when
anonymising is off. An absence check passes on a 404 body and on an empty fixture.
"""

from __future__ import annotations

import ast
import json
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

import contextlake.kb.dashboard.server as dashboard_server
from contextlake.kb import router
from contextlake.kb.cmds.docs import API_DIR, DESIGN_DIR
from contextlake.kb.dashboard.chat import _withhold, chat_answer
from contextlake.kb.dashboard.server import build_dashboard_server
from contextlake.kb.dashboard.site import build_dashboard_site
from contextlake.kb.docs.stamp import stamp
from contextlake.kb.ids import make_id
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.server import AskOut
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize import repo_slug

# Only reaches a response through repo prose (README, wiki page, generated document).
SENTINEL = "SENTINEL-PROSE-7f3a"
AUTHOR = "Wilhelmina Testerson"
INTERNAL_HOST = "internal.example.invalid"
# Everything that must be absent from an anonymised response. The e-mail is here because
# it is what the pseudonym is hashed from, and it must not travel either.
NEEDLES = (SENTINEL, "Testerson", "Wilhelmina", INTERNAL_HOST, "w@example.invalid")

REPO = "demo/app"
REPO_URL = "demo%2Fapp"
PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 1, 1))


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Config discovery walks up from the current directory for a `.contextlake.kb.toml`,
    and the dashboard's settings and MCP routes call it. Start the walk in a temp dir, so
    no config above this checkout is read."""
    monkeypatch.chdir(tmp_path)


def _prose(where: str) -> str:
    return f"{SENTINEL} {where}, written by {AUTHOR}. See https://{INTERNAL_HOST}/handbook\n"


def _make_store(tmp_path: Path):
    """A store with one repo whose README, wiki page and generated documents all carry
    prose, and whose git history has one named author. Returns ``(store, store_dir)``."""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (clone / "README.md").write_text(f"# {REPO}\n\n" + _prose("README"), encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": AUTHOR, "GIT_AUTHOR_EMAIL": "w@example.invalid",
           "GIT_COMMITTER_NAME": AUTHOR, "GIT_COMMITTER_EMAIL": "w@example.invalid",
           "PATH": "/usr/bin:/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(clone), *cmd], check=True, env=env,
                       capture_output=True)

    s = SqliteStore(store_dir / "index.sqlite")
    nodes = [
        Node(id=make_id("repo", REPO), repo=REPO, kind="repo", name=REPO),
        Node(id="demo_svc", repo=REPO, kind="class", name="Svc", file="a.py", lang="python"),
        Node(id="demo_run", repo=REPO, kind="function", name="run", file="a.py",
             lang="python"),
    ]
    edges = [Edge(src="demo_run", dst="demo_svc", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=PROV)]
    s.upsert_repo(Repo(id=REPO, path=str(clone), head_commit="h1"))
    write_shard(store_dir, GraphShard(repo=REPO, head_commit="h1", nodes=nodes, edges=edges))
    reindex_shard(s, store_dir, REPO)
    s.mark_indexed(REPO, "h1", "2026-06-01T00:00:00Z")

    wiki = store_dir / "wiki"
    wiki.mkdir()
    (wiki / (repo_slug(REPO) + ".md")).write_text(
        f"# {REPO}\n\n" + _prose("wiki"), encoding="utf-8")
    for dirs, kind in ((API_DIR, "api"), (DESIGN_DIR, "design")):
        d = store_dir.joinpath(*dirs)
        d.mkdir(parents=True)
        lines = [f"# {REPO} {kind}", "", *stamp(kind, REPO, "h1"), _prose(kind), ""]
        (d / (repo_slug(REPO) + ".md")).write_text("\n".join(lines), encoding="utf-8")
    return s, store_dir


# --- S07: the static export ---------------------------------------------------------

# A real export writes 14 files here (13 anonymised). A walk that finds fewer has not
# walked the export, and an empty walk would pass every absence check below.
_MIN_EXPORT_FILES = 12


def _export_files(out: Path) -> list[Path]:
    return sorted(p for p in out.rglob("*") if p.is_file())


def _leaks_in(out: Path, needles=NEEDLES) -> list[tuple[str, str]]:
    """``(relative path, needle)`` for every needle in every file under ``out``. The walk
    is the output directory, not a list of files somebody thought of."""
    found = []
    for p in _export_files(out):
        raw = p.read_bytes()
        for needle in needles:
            if needle.encode("utf-8") in raw:
                found.append((str(p.relative_to(out)), needle))
    return found


def test_a_plain_export_does_carry_the_wiki_page(tmp_path):
    """The half that makes the export tests below mean something."""
    s, store_dir = _make_store(tmp_path)
    s.close()
    out = build_dashboard_site(store_dir, tmp_path / "site", anonymize=False)
    assert len(_export_files(out)) >= _MIN_EXPORT_FILES
    assert (out / "graph" / ("wiki-" + repo_slug(REPO) + ".html")).is_file()
    assert _leaks_in(out, (SENTINEL,))


def test_an_anonymized_export_holds_no_prose_and_no_name_in_any_file(tmp_path):
    s, store_dir = _make_store(tmp_path)
    s.close()
    out = build_dashboard_site(store_dir, tmp_path / "site", anonymize=True)
    assert len(_export_files(out)) >= _MIN_EXPORT_FILES, (
        "the walk found too few files to be a walk of the export")
    assert _leaks_in(out) == []


def test_an_anonymized_export_links_to_no_wiki_page(tmp_path):
    """Dropping the page and keeping the link would leave a dead "Read the wiki" control
    on every graph page and on the index."""
    s, store_dir = _make_store(tmp_path)
    s.close()
    out = build_dashboard_site(store_dir, tmp_path / "site", anonymize=True)
    page = "wiki-" + repo_slug(REPO) + ".html"
    assert [p for p in _export_files(out) if p.name == page] == []
    assert _leaks_in(out, (page,)) == []


def test_anonymizing_over_a_plain_export_removes_the_old_wiki_pages(tmp_path):
    """The folder is written in place, and `_refuse_foreign_dir` lets an export overwrite
    its own output. A plain export followed by an anonymised one, into the same folder,
    is the ordinary way to produce a shareable copy in a hurry."""
    s, store_dir = _make_store(tmp_path)
    s.close()
    out = tmp_path / "site"
    build_dashboard_site(store_dir, out, anonymize=False)
    assert _leaks_in(out, (SENTINEL,)), "the plain export did not carry the wiki page"
    build_dashboard_site(store_dir, out, anonymize=True)
    assert len(_export_files(out)) >= _MIN_EXPORT_FILES
    assert _leaks_in(out) == []


# --- S08: the Chat route, and every other route -------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve(tmp_path):
    """``serve(anonymize=...)`` -> ``(base_url, store, store_dir)``. Mutating routes are
    ON so the routes gated behind them are reachable; none of them is driven below."""
    started = []

    def _serve(*, anonymize: bool):
        store, store_dir = _make_store(tmp_path)
        port = _free_port()
        srv = build_dashboard_server(store, store_dir, host="127.0.0.1", port=port,
                                     anonymize=anonymize, allow_mutations=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append((srv, store))
        return f"http://127.0.0.1:{port}", store, store_dir

    yield _serve
    for srv, store in started:
        srv.shutdown()
        srv.server_close()
        store.close()


@dataclass(frozen=True)
class Req:
    method: str
    url: str
    body: dict | None = None
    # True when this request returns prose or a real name with anonymising OFF. The
    # same request is then asserted clean with it on.
    carrier: bool = False


def _fetch(base: str, req: Req) -> tuple[int, str]:
    data = None if req.body is None else json.dumps(req.body).encode("utf-8")
    r = urllib.request.Request(
        base + req.url, data=data, method=req.method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# One question per rule in `router._RULES`, plus the open-question fallback. The test
# below fails when a rule has no question, so a rule added later cannot slip past.
_CHAT_QUESTIONS = {
    router.IMPACT: "what is the blast radius of `Svc`",
    router.OWNERS: f"who owns `{REPO}`",
    router.SUBCLASSES: "what are the subclasses of `Svc`",
    router.DEPENDENTS: f"what depends on `{REPO}`",
    router.CALLERS: "who calls `Svc`",
    router.DEFINITION: "where is `Svc` defined",
    router.EXPLAIN: f"explain `{REPO}`",
    router.SEARCH: "quarterly forecast numbers",
}
_CHAT_CARRIERS = {router.OWNERS, router.EXPLAIN}


def _chat(route: str) -> Req:
    return Req("POST", "/api/chat", {"question": _CHAT_QUESTIONS[route]},
               carrier=route in _CHAT_CARRIERS)


_NODE = "node=demo_svc"

# Every route the server dispatches on, keyed by the string it dispatches on, with the
# requests that drive it. A key is a string found in `build_dashboard_server`'s source by
# `_discovered_routes`, so this table cannot fall behind the server: a route added there
# and not listed here fails `test_every_dispatched_route_is_driven_or_excused`.
_DRIVEN: dict[str, list[Req]] = {
    "/": [Req("GET", "/")],
    "/index.html": [Req("GET", "/index.html")],
    "/dashboard.html": [Req("GET", "/dashboard.html")],
    "dashboard.js": [Req("GET", "/dashboard.js")],
    "dashboard.css": [Req("GET", "/dashboard.css")],
    "mermaid.min.js": [Req("GET", "/mermaid.min.js")],
    "/neighbors": [Req("GET", "/neighbors?id=demo_svc")],
    "/graph/": [Req("GET", "/graph/")],
    "overview": [Req("GET", "/graph/overview")],
    "overview.html": [Req("GET", "/graph/overview.html")],
    "index.html": [Req("GET", "/graph/index.html")],
    "neighbors": [Req("GET", "/graph/neighbors?id=demo_svc")],
    "repo-": [Req("GET", f"/graph/repo-{repo_slug(REPO)}.html")],
    "/api/capabilities": [Req("GET", "/api/capabilities")],
    "/api/groups": [Req("GET", "/api/groups")],
    "/api/health": [Req("GET", "/api/health")],
    "/api/overview": [Req("GET", "/api/overview")],
    "/api/relationships": [Req("GET", "/api/relationships")],
    "/api/impact": [Req("GET", f"/api/impact?{_NODE}")],
    "/api/impact/diagram": [Req("GET", f"/api/impact/diagram?{_NODE}")],
    "/api/path": [Req("GET", "/api/path?from=demo_run&to=demo_svc")],
    "/api/search": [Req("GET", "/api/search?q=Svc")],
    "/api/mcp": [Req("GET", "/api/mcp")],
    "/api/settings": [Req("GET", "/api/settings")],
    "/api/wiki/status": [Req("GET", "/api/wiki/status")],
    "/api/docs/status": [Req("GET", "/api/docs/status")],
    "/api/wiki/estimate": [Req("GET", "/api/wiki/estimate")],
    "/api/repo/": [Req("GET", f"/api/repo/{REPO_URL}", carrier=True)],
    "/rel": [Req("GET", f"/api/repo/{REPO_URL}/rel")],
    "/data-flow": [Req("GET", f"/api/repo/{REPO_URL}/data-flow")],
    "/diagram": [Req("GET", f"/api/repo/{REPO_URL}/diagram")],
    "/modules": [Req("GET", f"/api/repo/{REPO_URL}/modules")],
    "/wiki": [Req("GET", f"/api/repo/{REPO_URL}/wiki", carrier=True)],
    "/docs": [Req("GET", f"/api/repo/{REPO_URL}/docs?kind=api", carrier=True),
              Req("GET", f"/api/repo/{REPO_URL}/docs?kind=design", carrier=True)],
    "/api/chat": [_chat(r) for r in _CHAT_QUESTIONS],
}

# Dispatch strings that are not routes of their own, and routes that cannot be driven
# here. Each has a reason, so an entry nobody can justify is easy to spot.
_EXCUSED: dict[str, str] = {
    "": "the empty leaf of /graph/, the same page as the '/graph/' request",
    ".html": "strips the suffix off a repo page name; driven through 'repo-'",
    "/api/": "the prefix that hands a request to the /api handler; each route under it "
             "is its own entry",
    "/api/wiki/generate": "POST, needs the mutation token, and starts a real "
                          "`contextlake` child process; answers with run state",
    "/api/docs/generate": "POST, needs the mutation token, and starts a real "
                          "`contextlake` child process; answers with run state",
    "/api/mcp/serve": "POST, needs the mutation token, and starts a real MCP server "
                      "process; answers with run state",
    "/api/repo/add": "POST, needs the mutation token, and clones a repository from a "
                     "URL over the network; answers with run state",
    "/sync": "POST, needs the mutation token, and runs git against the repo's clone; "
             "answers with run state",
}

_ROUTE_RECEIVERS = {"path", "rest", "leaf", "slug", "asset"}


def _is_route_receiver(node) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _ROUTE_RECEIVERS
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "parsed" and node.attr == "path")


def _strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for el in node.elts:
            yield from _strings(el)


def _discovered_routes() -> set[str]:
    """Every string `build_dashboard_server` dispatches on.

    The server has no route table. It dispatches with `path == ...`, `path.startswith`,
    `rest.endswith` and a dict of static assets, inside nested functions. Those
    comparisons are the table, so this reads them out of the source. A string is taken
    when it is compared on a variable that holds a request path, or when it starts with
    a slash, whatever it is compared on.
    """
    tree = ast.parse(Path(dashboard_server.__file__).read_text(encoding="utf-8"))
    builder = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "build_dashboard_server")
    found: set[str] = set()
    for n in ast.walk(builder):
        if isinstance(n, ast.Compare) and isinstance(n.ops[0], (ast.Eq, ast.In)):
            for s in (x for c in n.comparators for x in _strings(c)):
                if _is_route_receiver(n.left) or s.startswith("/"):
                    found.add(s)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in ("startswith", "endswith") and n.args):
            for s in _strings(n.args[0]):
                if _is_route_receiver(n.func.value) or s.startswith("/"):
                    found.add(s)
        elif (isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
              and any(isinstance(t, ast.Name) and t.id == "assets" for t in n.targets)):
            for k in n.value.keys:
                found.update(_strings(k))
    return found


def test_the_route_extractor_finds_the_routes_it_exists_to_find():
    """A scan that finds nothing makes the next test pass for nothing."""
    found = _discovered_routes()
    assert {"/api/chat", "/api/repo/", "/wiki", "/docs", "/graph/", "/api/overview"} <= found
    assert len(found) >= 30


def test_every_dispatched_route_is_driven_or_excused():
    found = _discovered_routes()
    listed = set(_DRIVEN) | set(_EXCUSED)
    assert found - listed == set(), (
        f"the server dispatches on {sorted(found - listed)}, which this file neither drives "
        f"nor excuses. Add a request to _DRIVEN, or a reason to _EXCUSED.")
    assert listed - found == set(), (
        f"{sorted(listed - found)} are listed here and no longer in the server's source. "
        f"Remove them.")
    assert not set(_DRIVEN) & set(_EXCUSED)


def test_every_chat_rule_has_a_question():
    names = {name for name, _pattern in router._RULES} | {router.SEARCH}
    assert names == set(_CHAT_QUESTIONS)
    for route, question in _CHAT_QUESTIONS.items():
        assert router.classify(question)[0] == route, (
            f"{question!r} no longer reaches the {route!r} route")


def test_every_field_of_the_ask_answer_is_either_scrubbed_or_known_safe():
    """`chat_answer` withholds two fields of `AskOut`. A field added later that carries
    prose or a name would pass through untouched, so each one is classified here."""
    scrubbed = {"wiki", "owners"}
    safe = {
        "question", "route", "target", "note", "answered", "truncated",  # scalars
        "nodes", "blast",   # symbols, with the docstrings every dashboard route serves
        "brief",            # the repo's anatomy: counts, kinds, packages, top symbols
    }
    assert set(AskOut.model_fields) == scrubbed | safe, (
        f"AskOut changed: {sorted(set(AskOut.model_fields) ^ (scrubbed | safe))}. Decide "
        f"whether the new field carries prose or a name, and scrub it in chat._withhold "
        f"if it does.")


def test_the_routes_that_carry_prose_do_carry_it_without_anonymize(serve):
    """The half that makes the sweep below mean something: each carrier is a real one."""
    base, _store, _dir = serve(anonymize=False)
    for key, reqs in _DRIVEN.items():
        for req in reqs:
            if not req.carrier:
                continue
            status, body = _fetch(base, req)
            assert status == 200, f"{key}: {req.url} answered {status}"
            assert SENTINEL in body or "Testerson" in body, (
                f"{req.url} {req.body or ''} carries neither the prose nor the name with "
                f"anonymising off, so its anonymised check proves nothing. Fix the fixture.")


def test_no_route_serves_prose_or_a_real_name_when_anonymized(serve):
    base, _store, _dir = serve(anonymize=True)
    problems = []
    for key, reqs in _DRIVEN.items():
        for req in reqs:
            status, body = _fetch(base, req)
            label = f"{key}: {req.method} {req.url} {req.body or ''}".strip()
            # A mistyped URL answers 404 with a short JSON body, which holds no needle.
            if status != 200:
                problems.append(f"{label} answered {status}, so it was not checked")
            if not body.strip():
                problems.append(f"{label} answered an empty body, so it was not checked")
            problems += [f"{label} leaked {n!r}" for n in NEEDLES if n in body]
    assert problems == []


def test_chat_pseudonymises_owners_the_way_the_owners_panel_does(serve):
    """One person, one pseudonym, on both. Re-hashing the router's names would give a
    different one, since the pseudonym is keyed on the e-mail the router does not carry.
    The names must also be there: a scrub that emptied the list would pass the sweep."""
    base, _store, _dir = serve(anonymize=True)
    _status, detail = _fetch(base, Req("GET", f"/api/repo/{REPO_URL}"))
    panel = {o["name"] for o in json.loads(detail)["owners"]}
    _status, answer = _fetch(base, _chat(router.OWNERS))
    owners = json.loads(answer)["structured"]["owners"]["owners"]
    names = {o["name"] for o in owners}
    assert names and names == panel
    assert all(n.startswith("Contributor ") for n in names)


def test_chat_keeps_the_wiki_flags_and_says_the_text_is_withheld(serve):
    base, _store, _dir = serve(anonymize=True)
    _status, answer = _fetch(base, _chat(router.EXPLAIN))
    structured = json.loads(answer)["structured"]
    assert structured["wiki"]["found"] is True
    assert structured["wiki"]["markdown"] == ""
    assert "withheld" in structured["note"]


def test_the_llm_never_receives_prose_or_a_real_name_when_anonymized(tmp_path):
    """`--llm-chat` sends the router's result to the configured provider. The scrub runs
    before the prompt is built, so the provider sees none of it."""
    class Stub:
        def __init__(self):
            self.prompts = []

        def generate(self, prompt, *, system=None):
            self.prompts.append(prompt)
            return "ok"

    store, _dir = _make_store(tmp_path)
    try:
        for anonymize in (False, True):
            llm = Stub()
            for route in (router.EXPLAIN, router.OWNERS):
                chat_answer(store, _CHAT_QUESTIONS[route], llm=llm, anonymize=anonymize)
            joined = "\n".join(llm.prompts)
            assert len(llm.prompts) == 2
            if anonymize:
                assert [n for n in NEEDLES if n in joined] == []
            else:
                assert SENTINEL in joined and "Testerson" in joined, (
                    "the prompt carried neither with anonymising off, so the clean prompt "
                    "above proves nothing")
    finally:
        store.close()


def test_owner_names_are_dropped_when_no_pseudonym_can_be_derived(tmp_path):
    """Fail closed, as the MCP network path does for a key that wants pseudonyms: no
    names, and never the real ones. Here the scope is a repo the store does not hold."""
    store, _dir = _make_store(tmp_path)
    try:
        structured = {
            "route": "owners", "note": "n",
            "owners": {"scope": "not/indexed", "found": True, "ranking_gap": None,
                       "owners": [{"name": AUTHOR, "commits": 1, "lines": 2,
                                   "last_active": "2026-01-01", "share": 1.0}]},
        }
        out = _withhold(store, structured)
        assert out["owners"]["owners"] == []
        assert out["owners"]["ranking_gap"]
        assert AUTHOR not in json.dumps(out)
    finally:
        store.close()


def test_an_answer_of_an_unknown_shape_is_withheld_not_passed_through(tmp_path):
    store, _dir = _make_store(tmp_path)
    try:
        out = _withhold(store, [AUTHOR, SENTINEL])
        assert out["answered"] is False
        assert AUTHOR not in json.dumps(out) and SENTINEL not in json.dumps(out)
    finally:
        store.close()
