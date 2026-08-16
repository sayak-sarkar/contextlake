"""`--anonymize` must cover the SERVED dashboard, not only the static export.

Before this, `anonymize` appeared nowhere in `dashboard/server.py`: the flag existed on
`--site` only. The live dashboard therefore rendered real contributor names with
per-person activity — the surface most likely to be screen-shared — with no way to turn
that off. `dashboard/data.py` already took an `anonymize` argument on every function that
carries an identity; the server simply never passed it.

Driven over real HTTP rather than by calling the data layer directly, because the defect
was entirely in the wiring between the two. A test that called `repo_detail(anonymize=True)`
would have passed for the whole time the bug existed.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import urllib.request
from datetime import date

import pytest

from contextlake.kb.dashboard.server import build_dashboard_server
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 1, 1))
AUTHOR = "Wilhelmina Testerson"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(tmp_path, monkeypatch, *, anonymize: bool):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": AUTHOR, "GIT_AUTHOR_EMAIL": "w@example.invalid",
           "GIT_COMMITTER_NAME": AUTHOR, "GIT_COMMITTER_EMAIL": "w@example.invalid",
           "PATH": "/usr/bin:/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env,
                       capture_output=True)

    s = SqliteStore(tmp_path / "index.sqlite")
    nodes = [Node(id="svc", repo="team/app", kind="class", name="Svc", lang="python",
                  file="a.py", line_start=1)]
    edges = [Edge(src="svc", dst="svc", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=_PROV)]
    s.upsert_repo(Repo(id="team/app", path=str(repo), head_commit="h1"))
    write_shard(tmp_path, GraphShard(repo="team/app", head_commit="h1",
                                     nodes=nodes, edges=edges))
    s.upsert_nodes("team/app", nodes)
    s.upsert_edges("team/app", edges)

    port = _free_port()
    srv = build_dashboard_server(s, tmp_path, host="127.0.0.1", port=port,
                                 anonymize=anonymize)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}", s


def _get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:  # noqa: S310
        return json.loads(r.read().decode())


@pytest.mark.parametrize("anonymize", [False, True])
def test_the_served_repo_detail_honours_anonymize(tmp_path, monkeypatch, anonymize):
    """THE PAIR. Without the flag the real name is served (today's behaviour, kept);
    with it, the name must be gone. Asserting only the anonymised half would pass on a
    store that happened to have no owners at all."""
    srv, base, store = _serve(tmp_path, monkeypatch, anonymize=anonymize)
    try:
        body = json.dumps(_get(base, "/api/repo/team%2Fapp"))
    finally:
        srv.shutdown()
        store.close()

    if anonymize:
        assert AUTHOR not in body, (
            "the served dashboard rendered a real contributor name with --anonymize on")
    else:
        assert AUTHOR in body, (
            "the fixture produced no owner at all, so the anonymised case above proves "
            "nothing — check the git fixture before trusting this pair")


# A string that can only reach a response through the WIKI PAGE's prose. Distinct from
# AUTHOR, which arrives through git, so a test that finds this one has proved the wiki
# body specifically travelled, not merely that some identity did.
WIKI_SECRET = "Reviewed by Wilhelmina Testerson, internal.example.invalid"


def _with_wiki(tmp_path, monkeypatch, *, anonymize: bool):
    srv, base, store = _serve(tmp_path, monkeypatch, anonymize=anonymize)
    from contextlake.kb.visualize import repo_slug

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / (repo_slug("team/app") + ".md")).write_text(
        f"# team/app\n\n{WIKI_SECRET}\n", encoding="utf-8")
    return srv, base, store


# Every served route that can carry a wiki page's prose. Listed rather than discovered,
# and the pair of assertions below is what keeps the list honest: a route added later
# that serves the same bytes and is not here simply goes unchecked, so the reason for
# each entry is written next to it.
_PROSE_ROUTES = [
    # carries `wiki.html` alongside the brief, README and owners
    "/api/repo/team%2Fapp",
    # serves the SAME page on its own, for the Wiki tab's module picker. This is the one
    # that leaked: `repo_detail` dropped the body and this route had no `anonymize`
    # parameter at all, so the prose the other route withheld was one request away.
    "/api/repo/team%2Fapp/wiki",
]


@pytest.mark.parametrize("route", _PROSE_ROUTES)
def test_no_route_serves_wiki_prose_when_anonymized(tmp_path, monkeypatch, route):
    srv, base, store = _with_wiki(tmp_path, monkeypatch, anonymize=True)
    try:
        payload = _get(base, route)
    finally:
        srv.shutdown()
        store.close()
    body = json.dumps(payload)
    assert WIKI_SECRET not in body, f"{route} served wiki prose with --anonymize on"
    assert "Testerson" not in body, f"{route} served an author name with --anonymize on"
    # The FLAGS must survive. Dropping the whole object would hide that a page exists at
    # all, which is a fact about the repo rather than about a person, and the Wiki tab
    # needs it to tell "anonymised" from "never generated".
    wiki = payload.get("wiki", payload)
    assert wiki.get("found") is True, (
        f"{route} lost the wiki `found` flag; anonymising must drop the PROSE, not the "
        f"knowledge that a page exists: {wiki}")


@pytest.mark.parametrize("route", _PROSE_ROUTES)
def test_each_of_those_routes_really_does_carry_the_prose_otherwise(
        tmp_path, monkeypatch, route):
    """The half that makes the half above mean something.

    Every assertion up there is a NOT-in check, and a not-in check passes against a route
    that returns nothing, a 404 body, or a wiki nobody generated. This asserts the same
    routes serve the secret when anonymising is off, so each one is known to be a real
    carrier before it is asserted clean.
    """
    srv, base, store = _with_wiki(tmp_path, monkeypatch, anonymize=False)
    try:
        body = json.dumps(_get(base, route))
    finally:
        srv.shutdown()
        store.close()
    assert WIKI_SECRET in body, (
        f"{route} did not carry the wiki prose even with anonymising OFF, so its "
        f"anonymised assertion proves nothing. Fix the fixture, not the assertion.")
