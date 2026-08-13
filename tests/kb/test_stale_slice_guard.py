"""The stale-slice guard: does a returned citation still describe the file on disk?

The measurement this file has to make is a **near-miss**, not a smoke test. A guard that
reports drift for every node passes any test that only ever edits a file, and is exactly as
useless as one that reports it for none: an agent that is told every citation is suspect
stops reading the flag. So every drift case here is paired with a case that must NOT be
reported as drifted, over the same fixture:

* an untouched file, and a file whose mtime moved with its content unchanged (a no-op save,
  a branch checkout, a re-clone -- the false positive an mtime-only guard cannot avoid),
  both of which must come back ``verified``;
* the same file with twenty lines inserted above the cited symbol, which must come back
  ``stale``;
* a repo with no readable checkout, which must come back ``unverifiable`` -- asserted
  explicitly to be neither of the other two, because "I could not check" collapsing into
  "fine" is a defect class this package has shipped before.

Every assertion here fails without the guard, with ``citation_status`` null.

mtimes are set with :func:`os.utime` rather than by really editing and hoping: the verdict
turns on a nanosecond comparison, and a test whose outcome depends on the host filesystem's
timestamp granularity measures the filesystem.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from mcp import Client

from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.server import build_server
from contextlake.kb.store.drift import _NOTES, DriftProbe, parse_indexed_at
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "team/api"
SOURCE = "mod.py"
INDEXED_AT = "2026-01-01T00:00:00+00:00"
_BASE_NS = parse_indexed_at(INDEXED_AT)


def _at(offset_seconds: float) -> tuple[int, int]:
    """(atime, mtime) in ns, ``offset_seconds`` either side of the index timestamp."""
    ns = int(_BASE_NS + offset_seconds * 1_000_000_000)
    return (ns, ns)


def _clone(tmp_path, body: str = "def alpha():\n    return 1\n"):
    root = tmp_path / "clone"
    root.mkdir(exist_ok=True)
    (root / SOURCE).write_text(body, encoding="utf-8")
    # Older than the index by an hour: the ordinary case, and the one that must be free.
    os.utime(root / SOURCE, ns=_at(-3600))
    return root


def _seed(store, root, *, indexed: bool = True, path: str | None = None) -> None:
    store.upsert_repo(Repo(id=REPO, path=str(root) if path is None else path))
    store.upsert_nodes(REPO, [
        Node(id="n1", repo=REPO, kind="function", name="alpha", file=SOURCE,
             line_start=1, line_end=2, lang="python"),
        Node(id="nofile", repo=REPO, kind="function", name="beta"),
    ])
    store.upsert_edges(REPO, [Edge(
        src="n1", dst="nofile", relation="calls", confidence=Confidence.EXTRACTED,
        provenance=Provenance(source_file=SOURCE, source_line=1,
                              verified_at=date(2026, 1, 1)))])
    if indexed:
        store.mark_indexed(REPO, "head1", INDEXED_AT, "4")


@pytest.fixture
def clone(tmp_path):
    return _clone(tmp_path)


@pytest.fixture
def store(tmp_path, clone):
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s, clone)
    yield s
    s.close()


def _check(store, node_id="n1", **kw):
    probe = DriftProbe(store, **kw)
    return probe, probe.check(store.get_node(node_id))


# --- the pair that makes this a measurement ------------------------------------------

def test_untouched_file_is_verified(store):
    _, check = _check(store)
    assert check.status == "verified"
    assert check.note is None


def test_mtime_moved_but_content_did_not_is_verified(store, clone):
    """The false positive an mtime-only guard cannot avoid.

    A no-op save, `git checkout` to another branch and back, or a re-clone rewrites the
    mtime of a file whose bytes never changed. Across a mirrored fleet that is most files
    at once, so an mtime-only guard would flag nearly every citation in one go. The
    confirming read is what buys this case back -- and it is why the gate can afford to be
    as blunt as it is.
    """
    os.utime(clone / SOURCE, ns=_at(+3600))
    probe, check = _check(store)
    assert check.status == "verified"
    assert probe.stats.escalated == 1  # the gate flagged it; the read cleared it


def test_lines_inserted_above_the_symbol_is_stale(store, clone):
    body = "\n".join(f"# padding {i}" for i in range(20)) + "\ndef alpha():\n    return 1\n"
    (clone / SOURCE).write_text(body, encoding="utf-8")
    os.utime(clone / SOURCE, ns=_at(+3600))
    _, check = _check(store)
    assert check.status == "stale"
    assert check.reason == "name_absent"
    assert "moved" in check.note


# --- the third state ------------------------------------------------------------------

def test_missing_checkout_is_unverifiable_not_verified(tmp_path):
    """The collapse this project keeps having to fix: a check that never ran, reported as
    a pass. Asserted against both of the other two statuses by name, so a future refactor
    that defaults to either one fails here."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s, None, path=str(tmp_path / "no-such-clone"))
    _, check = _check(s, "n1")
    assert check.status == "unverifiable"
    assert check.status not in ("verified", "stale")
    assert check.reason == "checkout_missing"
    assert "NOT checked" in check.note
    s.close()


def test_registered_but_never_indexed_is_unverifiable(tmp_path, clone):
    """`upsert_repo` does not write `indexed_at` -- only `mark_indexed` does. With no
    baseline there is nothing to compare an mtime against, and inventing one would certify
    every file under it."""
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s, clone, indexed=False)
    _, check = _check(s)
    assert check.status == "unverifiable"
    assert check.reason == "index_time_unknown"
    s.close()


def test_deleted_file_is_stale_not_unverifiable(store, clone):
    """A checkout that exists with the file gone is a fact about the graph, not a gap in
    the machine -- the opposite reading from `checkout_missing` above."""
    (clone / SOURCE).unlink()
    _, check = _check(store)
    assert check.status == "stale"
    assert check.reason == "file_missing"


def test_node_without_a_citation_is_not_checked(store):
    """No file, no line, nothing that could have moved. Not `verified` (nothing was
    checked) and not `stale` (there is no citation to be stale)."""
    _, check = _check(store, "nofile")
    assert check is None


def test_file_node_whose_contents_changed_is_unverifiable(store, clone):
    """A `file` node's citation is the path, and the path is still right -- but what the
    graph records about its CONTENTS came from an older version, and confirming that would
    mean re-parsing on the serving path. So: not stale, and not verified either."""
    store.upsert_nodes(REPO, [Node(id="f1", repo=REPO, kind="file", name=SOURCE,
                                   file=SOURCE)])
    os.utime(clone / SOURCE, ns=_at(+3600))
    _, check = _check(store, "f1")
    assert check.status == "unverifiable"
    assert check.reason == "content_unchecked"


def test_file_node_of_an_untouched_file_is_verified(store):
    """The pair to the case above: the same node kind, the same code path, no drift."""
    store.upsert_nodes(REPO, [Node(id="f1", repo=REPO, kind="file", name=SOURCE,
                                   file=SOURCE)])
    _, check = _check(store, "f1")
    assert check.status == "verified"


# --- cost -----------------------------------------------------------------------------

def test_one_stat_per_distinct_file_not_per_node(store, clone):
    """The bounded-cost claim, as an assertion rather than a sentence in a report."""
    store.upsert_nodes(REPO, [
        Node(id=f"s{i}", repo=REPO, kind="function", name="alpha", file=SOURCE,
             line_start=1, lang="python")
        for i in range(50)])
    probe = DriftProbe(store)
    for i in range(50):
        probe.check(store.get_node(f"s{i}"))
    assert probe.stats.checked == 50
    assert probe.stats.statted == 1          # one distinct file
    assert probe.stats.escalated == 0        # nothing was modified: no reads at all


def test_repeated_node_is_answered_from_cache(store):
    probe = DriftProbe(store)
    node = store.get_node("n1")
    probe.check(node)
    probe.check(node)
    assert probe.stats.checked == 1
    assert probe.stats.cache_hits == 1


def test_confirmation_budget_caps_the_reads_and_says_so(store, clone):
    """Past the cap the gate's own verdict is served, labelled unconfirmed. Silently
    stopping would report a clean bill of health for work that never happened."""
    os.utime(clone / SOURCE, ns=_at(+3600))
    probe, check = _check(store, confirm_budget=0)
    assert check.status == "stale"
    assert check.reason == "modified_after_index"
    assert probe.stats.escalated == 0 and probe.stats.budget_spent == 1


# --- the guard must never be able to break an answer ----------------------------------

class _ExplodingStore:
    """A store whose repo lookup fails, which the guard must survive."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_repo(self, repo_id):
        raise RuntimeError("boom")


def test_probe_failure_degrades_to_unverifiable(store):
    """`test_server_contract.py` exists because one handler regressing to a raise costs
    every later answer in the session, not just this one. This guard adds filesystem I/O
    to the path of every node-returning tool, so its failure mode has to be a disclosure."""
    _, check = _check(_ExplodingStore(store))
    assert check.status == "unverifiable"
    assert check.reason == "probe_error"


# --- over the wire --------------------------------------------------------------------

async def _call(server, tool, args):
    async with Client(server) as client:
        return await client.call_tool(tool, args)


def test_drift_is_disclosed_on_the_wire_and_the_answer_still_succeeds(store, clone):
    """Disclose, do not refuse: the drifted citation is still the best pointer available,
    so it comes back in a success-shaped response with the caveat attached."""
    body = "\n".join(f"# padding {i}" for i in range(20)) + "\ndef alpha():\n    return 1\n"
    (clone / SOURCE).write_text(body, encoding="utf-8")
    os.utime(clone / SOURCE, ns=_at(+3600))
    res = asyncio.run(_call(build_server(store), "find_definition", {"name": "alpha"}))
    assert res.is_error is False
    [node] = res.structured_content["result"]
    assert node["file"] == SOURCE and node["line_start"] == 1  # still cited
    assert node["citation_status"] == "stale"
    # The whole sentence, not a substring of it: every other text field on this model goes
    # through `sanitize_label`, which truncates at 256 characters, and a disclosure that
    # reaches the agent cut off mid-sentence would still pass an `in` check.
    assert node["citation_note"] == _NOTES["name_absent"]


def test_clean_result_on_the_wire_says_verified_and_carries_no_note(store):
    res = asyncio.run(_call(build_server(store), "find_definition", {"name": "alpha"}))
    [node] = res.structured_content["result"]
    assert node["citation_status"] == "verified"
    assert node["citation_note"] is None


@pytest.mark.parametrize("tool,args,key", [
    ("find_definition", {"name": "alpha"}, "result"),
    # Carries an edge and `as_edge_provenance`, so it takes a different branch of the
    # same funnel...
    ("find_callers", {"node_id": "nofile"}, "nodes"),
    # ...and this one is a plain scored search. Neither should change whether the
    # citation is weighed, but "they all go through `_node_out`" is a claim about the
    # code, and the point of a wire test is to stop it being only that.
    ("search_code", {"query": "alpha"}, "result"),
])
def test_every_node_returning_verb_carries_the_disclosure(store, clone, tool, args, key):
    body = "\n".join(f"# padding {i}" for i in range(20)) + "\ndef alpha():\n    return 1\n"
    (clone / SOURCE).write_text(body, encoding="utf-8")
    os.utime(clone / SOURCE, ns=_at(+3600))
    res = asyncio.run(_call(build_server(store), tool, args))
    assert res.is_error is False, tool
    nodes = [n for n in res.structured_content[key] if n["id"] == "n1"]
    assert nodes, f"{tool} returned no citable node, so this proved nothing"
    for n in nodes:
        assert n["citation_status"] == "stale", tool
        assert n["citation_note"] == _NOTES["name_absent"], tool


def test_blast_radius_hits_are_disclosed_too(store, clone):
    """`blast_radius` returns `ImpactHit`, not `Node`, so it bypasses `_node_out` and was
    the one verb handing back a file and a line with nothing said about either.

    That is worse than it sounds: an agent seeing `citation_status` on every other verb
    reads its absence here as "checked, fine" rather than as "not checked". The gap is
    invisible in isolation and only shows up beside the verbs that have the field, which
    is exactly why it needs a test rather than a reading."""
    body = "\n".join(f"# padding {i}" for i in range(20)) + "\ndef alpha():\n    return 1\n"
    (clone / SOURCE).write_text(body, encoding="utf-8")
    os.utime(clone / SOURCE, ns=_at(+3600))
    # `beta` has no citation of its own; `alpha` calls it, so alpha is the hit, and
    # alpha's citation is the one that just moved.
    res = asyncio.run(_call(build_server(store), "blast_radius", {"node_id": "nofile"}))
    assert res.is_error is False
    [hit] = res.structured_content["hits"]
    assert hit["id"] == "n1"
    assert hit["file"] == SOURCE and hit["line"] == 1     # still cited
    assert hit["citation_status"] == "stale"
    assert hit["citation_note"] == _NOTES["name_absent"]


def test_blast_radius_hit_on_an_untouched_file_says_verified(store):
    """The other half: the field is populated, not merely present-when-broken. Without
    this, wiring that returned None on the happy path would pass the test above."""
    res = asyncio.run(_call(build_server(store), "blast_radius", {"node_id": "nofile"}))
    [hit] = res.structured_content["hits"]
    assert hit["citation_status"] == "verified"
    assert hit["citation_note"] is None


def test_ask_shares_one_probe_across_its_legs(store, monkeypatch):
    """`ask` routes to several tools internally, and they must share one probe.

    Two things ride on it. A file cited by three legs costs one `stat()` rather than
    three, and the confirmation budget is drawn down once for the whole call rather than
    per leg. `serve.md` states both, and "one request" is easy to read as "one verb", so
    the shape is asserted rather than described: `bounded_tool` installs the probe and
    `ask` calls the other tools as bare functions, so exactly one is ever built.
    """
    import contextlake.kb.server as srv

    built = []
    real = srv.DriftProbe
    monkeypatch.setattr(srv, "DriftProbe",
                        lambda *a, **kw: built.append(1) or real(*a, **kw))
    res = asyncio.run(_call(build_server(store), "ask", {"question": "who calls beta"}))
    assert res.is_error is False
    assert built == [1], (
        f"{len(built)} probes built for one `ask`: the legs are not sharing one, so the "
        "stat cache and the confirmation budget are per-leg, not per-request")


def test_probe_is_per_request(store, clone):
    """Two calls, one file, and the file changes between them: the second call must not
    serve the first call's verdict. The caches are only sound for the instant the request
    is answered."""
    server = build_server(store)
    first = asyncio.run(_call(server, "find_definition", {"name": "alpha"}))
    assert first.structured_content["result"][0]["citation_status"] == "verified"
    (clone / SOURCE).write_text("# gone\n", encoding="utf-8")
    os.utime(clone / SOURCE, ns=_at(+3600))
    second = asyncio.run(_call(server, "find_definition", {"name": "alpha"}))
    assert second.structured_content["result"][0]["citation_status"] == "stale"


# --- the baseline the whole guard rests on --------------------------------------------

@pytest.mark.parametrize("stamp,expected_utc", [
    ("2026-01-01T00:00:00+00:00", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    # Naive: read as UTC, because `state.utcnow_iso` is the only writer that makes one.
    ("2026-01-01T00:00:00", datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ("2026-01-01T01:00:00+01:00", datetime(2026, 1, 1, tzinfo=timezone.utc)),
])
def test_index_timestamp_parses(stamp, expected_utc):
    assert parse_indexed_at(stamp) == int(expected_utc.timestamp() * 1_000_000_000)


@pytest.mark.parametrize("stamp", [None, "", "   ", "not-a-timestamp", "2026-13-45"])
def test_unreadable_index_timestamp_is_no_baseline_rather_than_a_guess(stamp):
    """`Z` is the interesting one above: `fromisoformat` rejects it before Python 3.11 and
    this package supports 3.10, so an unnormalised stamp would have made every repo
    stamped that way permanently unverifiable."""
    assert parse_indexed_at(stamp) is None


def test_a_stamp_in_the_future_still_verifies_older_files(store, clone):
    """Guards against the comparison being written the wrong way round -- an inverted
    `<=` passes every test above that uses an already-old file only by accident."""
    store.mark_indexed(REPO, "head1",
                       (datetime(2026, 1, 1, tzinfo=timezone.utc)
                        + timedelta(days=365)).isoformat(), "4")
    os.utime(clone / SOURCE, ns=_at(+3600))
    _, check = _check(store)
    assert check.status == "verified"
