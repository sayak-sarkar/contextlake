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
