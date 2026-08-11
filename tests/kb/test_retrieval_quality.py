"""The golden query set must stay scoreable, and its score must not regress.

`retrieval-quality.yml` scores this weekly. Weekly is the wrong latency for "the
fixture and the golden set have drifted apart": a renamed fixture symbol, a broken
JSON edit, or a retrieval change that quietly stops matching would sit green for up
to seven days, and a scheduled run nobody reads is barely better than no run.

So the same measurement gates every PR here. The floor is read from the golden set
rather than written twice, because a number in two places drifts -- the workflow
reads the identical field.

Kept cheap on purpose: the fixture is 42 nodes across 13 kinds, and only the offline
FTS retriever is scored. `semantic`/`hybrid` need a configured embedder, which a test
run cannot assume.
"""

import json
from pathlib import Path

from contextlake.kb.eval import evaluate, load_golden
from contextlake.kb.parse import index_repo_dir
from contextlake.kb.store.shards import write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = Path(__file__).resolve().parents[2]
FIXTURE_REPO = REPO / "examples" / "fixtures" / "eval-repo"
GOLDEN = REPO / "examples" / "fixtures" / "golden-queries.json"


def _indexed(tmp_path):
    shard = index_repo_dir(str(FIXTURE_REPO), "telemetry")
    write_shard(tmp_path, shard)
    store = SqliteStore(tmp_path / "index.sqlite")
    store.upsert_nodes("telemetry", shard.nodes)
    store.upsert_edges("telemetry", shard.edges)
    return store, shard


def test_the_fixture_still_produces_the_kinds_the_set_queries(tmp_path):
    """A golden set is only as good as the fixture behind it. If the parser stops
    emitting a kind, the queries for it would fail as a 'retrieval regression' when
    the real cause is upstream -- so assert the shape separately from the score."""
    _store, shard = _indexed(tmp_path)
    kinds = {n.kind for n in shard.nodes}
    # The five kinds 7.0.0 added, plus the non-code ones. Named explicitly: this is
    # the coverage claim the set rests on.
    for kind in ("field", "macro", "typedef", "enum_constant", "global_variable",
                 "class", "method", "table", "config_key"):
        assert kind in kinds, f"the eval fixture no longer produces {kind!r}"


def test_golden_set_scores_at_or_above_its_declared_floor(tmp_path):
    store, _shard = _indexed(tmp_path)
    golden = load_golden(GOLDEN)
    floor = json.loads(GOLDEN.read_text(encoding="utf-8"))["_floor_hit_rate"]

    result = evaluate(store, golden, k=10)

    assert result["n"] == len(golden)
    assert result["hit_rate"] >= floor, (
        f"retrieval hit-rate {result['hit_rate']} fell below the declared floor "
        f"{floor}; if this is a deliberate improvement, ratchet _floor_hit_rate up, "
        f"and never lower it to turn a run green")


def test_exact_name_queries_all_hit(tmp_path):
    """The set deliberately mixes exact names with natural-language phrasings, and
    FTS5 has no synonym matching, so the phrasings are EXPECTED to miss -- they are
    the measurable gap semantic search exists to close.

    That expectation is only meaningful if the exact-name half is solid. If an
    exact-name query starts missing, that is a real regression hiding inside an
    aggregate the phrasings already depress.
    """
    store, _shard = _indexed(tmp_path)
    golden = load_golden(GOLDEN)
    exact = [g for g in golden if " " not in g.query]
    result = evaluate(store, exact, k=10)
    assert result["hit_rate"] == 1.0, (
        "every exact-name query must hit; a miss here is a genuine retrieval defect")
