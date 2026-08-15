import json
from argparse import Namespace

import pytest

from contextlake.cli import main
from contextlake.kb.commands import cmd_eval
from contextlake.kb.eval import (
    GoldenQuery,
    evaluate,
    load_golden,
    make_fts_retriever,
    make_semantic_retriever,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from contextlake.kb.model import Node
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore


def test_metric_primitives():
    assert precision_at_k(["a", "b", "c"], ["a", "c"], 3) == pytest.approx(2 / 3)
    assert recall_at_k(["a", "b"], ["a", "c"], 2) == 0.5
    assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert reciprocal_rank(["x", "y"], ["a"]) == 0.0
    assert precision_at_k([], ["a"], 3) == 0.0          # no results
    assert recall_at_k(["a"], [], 1) == 0.0             # no expected


def test_evaluate_aggregates_with_a_stub_retriever():
    golden = [GoldenQuery("q1", ["a"]), GoldenQuery("q2", ["z"])]

    def stub(query, k, kind=None, repo=None):
        return {"q1": ["a", "b"], "q2": ["b", "c"]}[query]  # q1 hits @1, q2 misses

    r = evaluate(None, golden, k=2, retriever=stub)
    assert r["n"] == 2 and r["k"] == 2
    assert r["mrr"] == pytest.approx(0.5)               # (1.0 + 0.0) / 2
    assert r["hit_rate"] == 0.5
    assert r["precision@k"] == pytest.approx(0.25)      # (0.5 + 0.0) / 2
    assert r["recall@k"] == 0.5                         # (1.0 + 0.0) / 2


def test_load_golden(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"queries": [
        {"query": "x", "expected": ["a"]},
        {"query": "y", "expected": ["b"], "match": "name", "kind": "function"},
    ]}))
    g = load_golden(p)
    assert len(g) == 2
    assert g[0].query == "x" and g[0].match == "id"
    assert g[1].match == "name" and g[1].kind == "function"


def test_fts_retriever_scores_real_search(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("r", [
        Node(id="os", repo="r", kind="class", name="CatalogService"),
        Node(id="bh", repo="r", kind="class", name="BaggageHandler"),
    ])
    r = evaluate(s, [GoldenQuery("CatalogService", ["os"])], k=5,
                 retriever=make_fts_retriever(s))
    assert r["hit_rate"] == 1.0          # search finds the node we asked for
    assert r["mrr"] > 0.0
    s.close()


def test_match_by_name(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("r", [Node(id="n1", repo="r", kind="function", name="charge")])
    # expected by NAME ("charge"), not id — harness resolves retrieved ids -> names
    r = evaluate(s, [GoldenQuery("charge", ["charge"], match="name")], k=5)
    assert r["hit_rate"] == 1.0
    s.close()


def test_evaluate_reports_cost_dimension(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("r", [Node(id="os", repo="r", kind="class",
                              name="CatalogService", file="svc.py")])
    r = evaluate(s, [GoldenQuery("CatalogService", ["os"])], k=5)
    assert r["est_tokens_per_query"] > 0                 # returning a node costs tokens
    assert r["precision_per_1k_tokens"] > 0              # found it, at finite token cost
    assert "est_tokens" in r["per_query"][0]
    s.close()


def test_cmd_eval_marks_hits_and_misses_with_colored_glyphs(tmp_path, monkeypatch, gls_logs):
    """H3: the per-query ✓/✗ mark must come from style.ok()/style.fail() -- coloured
    when color is on, not a bare uncoloured glyph. FORCE_COLOR makes this
    discriminating: a bare "✓"/"✗" (the old code) would not carry the ANSI codes
    asserted below, so this fails against the pre-fix code and passes against the
    fix, unlike a plain-text glyph check which is identical either way."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n'
                   '\n[embeddings]\nenabled = false\n')

    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_nodes("r", [Node(id="os", repo="r", kind="class", name="CatalogService")])
    s.close()

    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"queries": [
        {"query": "CatalogService", "expected": ["os"]},          # hit
        {"query": "NoSuchThing", "expected": ["missing-id"]},   # miss
    ]}))

    args = Namespace(golden=str(golden), limit=None, retriever=None, config=str(cfg))
    assert cmd_eval(args) == 0

    # gls_logs.text is ANSI-stripped by pytest's LogCaptureHandler itself, so
    # read the raw record messages (log()'s actual argument) to see the codes.
    raw = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "\033[32m✓\033[0m CatalogService" in raw   # hit: style.ok(), green + reset
    assert "\033[31m✗\033[0m NoSuchThing" in raw    # miss: style.fail(), red + reset


def test_cmd_eval_usage_error_without_golden():
    assert cmd_eval(Namespace(golden=None, limit=None, retriever=None, config=None)) == 2


def test_cmd_eval_usage_error_json(capsys):
    args = Namespace(golden=None, limit=None, retriever=None, config=None, json=True)
    assert cmd_eval(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "missing_argument"


def test_cmd_eval_bad_golden_set_reports_and_exits_1(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    args = Namespace(golden=str(missing), limit=None, retriever=None, config=None)
    assert cmd_eval(args) == 1


def test_cmd_eval_bad_golden_set_json(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.json"
    args = Namespace(golden=str(missing), limit=None, retriever=None, config=None,
                     json=True)
    assert cmd_eval(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "bad_golden_set"


def test_cmd_eval_semantic_without_embeddings_configured_reports_and_exits_1(tmp_path):
    """`--retriever semantic` against a config with embeddings OFF must fail with the
    same actionable hint `kb embed` prints, not a stack trace, and must stay offline.

    The config now says `enabled = false` explicitly. It used to rely on the library
    default being off, so when that flipped in 7.7.0 the test silently stopped
    exercising the path its own name describes."""
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n'
                   '\n[embeddings]\nenabled = false\n')
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"queries": [{"query": "x", "expected": ["a"]}]}))

    args = Namespace(golden=str(golden), limit=None, retriever="semantic",
                     config=str(cfg))
    assert cmd_eval(args) == 1


def test_cmd_eval_semantic_without_embeddings_configured_json(tmp_path, capsys):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n'
                   '\n[embeddings]\nenabled = false\n')
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"queries": [{"query": "x", "expected": ["a"]}]}))

    args = Namespace(golden=str(golden), limit=None, retriever="semantic",
                     config=str(cfg), json=True)
    assert cmd_eval(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "embeddings_unavailable"


def test_cmd_eval_semantic_retriever_scores_with_a_fake_embedder(tmp_path, monkeypatch):
    """With an embedder available, `--retriever semantic` must build a vector
    store and actually score through it (not just hit the unavailable-hint
    branch covered above)."""
    import contextlake.kb.embeddings as emb_pkg

    class _FakeEmbedder:
        name = "fake"

        def embed(self, texts):
            return [[float(len(t)), 1.0] for t in texts]

    monkeypatch.setattr(emb_pkg, "build_embedder", lambda cfg: _FakeEmbedder())

    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n'
                   '\n[embeddings]\nenabled = false\n')
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_nodes("r", [Node(id="os", repo="r", kind="class", name="CatalogService")])
    s.close()

    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"queries": [{"query": "CatalogService", "expected": ["os"]}]}))

    args = Namespace(golden=str(golden), limit=None, retriever="semantic", config=str(cfg))
    # nothing was ever embedded into the vector store, so this scores a real
    # (empty) miss -- the point is exercising the build-vs-and-retrieve path,
    # not asserting a hit.
    assert cmd_eval(args) == 0


def test_cmd_eval_json_output_is_machine_readable(tmp_path, capsys):
    """The whole point of `--json`: a CI job can pipe stdout straight into a
    threshold check without scraping the colored, human-oriented log lines."""
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n'
                   '\n[embeddings]\nenabled = false\n')

    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_nodes("r", [Node(id="os", repo="r", kind="class", name="CatalogService")])
    s.close()

    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"queries": [
        {"query": "CatalogService", "expected": ["os"]},
    ]}))

    args = Namespace(golden=str(golden), limit=None, retriever=None, config=str(cfg),
                     json=True)
    assert cmd_eval(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["retriever"] == "fts"
    assert payload["hit_rate"] == 1.0
    assert payload["n"] == 1
    assert "per_query" in payload


def _store_and_golden(tmp_path):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n'
                   '\n[embeddings]\nenabled = false\n')
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_nodes("r", [Node(id="os", repo="r", kind="class", name="CatalogService")])
    s.close()
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"queries": [
        {"query": "CatalogService", "expected": ["os"]},
    ]}))
    return cfg, golden


def test_cli_kb_eval_without_json_flag_prints_human_readable_summary(tmp_path, capsys):
    """The real entry point, not a hand-built Namespace: `--json` uses
    `default=argparse.SUPPRESS` (`_S` in cli.py), so omitting the flag must
    leave `getattr(args, "json", False)` False and print the coloured
    `Eval [...]:` summary line, never a JSON blob -- the failure mode would be
    every plain `kb eval` invocation silently switching to JSON output."""
    cfg, golden = _store_and_golden(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["kb", "eval", "--config", str(cfg), "--golden", str(golden)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Eval [fts]:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_kb_eval_with_json_flag_prints_only_json(tmp_path, capsys):
    cfg, golden = _store_and_golden(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["kb", "eval", "--config", str(cfg), "--golden", str(golden), "--json"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)          # the whole of stdout must be valid JSON
    assert payload["hit_rate"] == 1.0


def test_make_semantic_retriever_binds_deps():
    # the factory closes over vector_store + embedder, so semantic is scorable —
    # the whole point of the refactor (the old fixed call site couldn't pass them)
    class FakeEmb:
        def embed(self, texts):
            return [[0.1, 0.2, 0.3]]

    class FakeVS:
        def search(self, vec, k=10, repo=None):
            return [("os", 0.9), ("bh", 0.4)]

    r = make_semantic_retriever(store=None, vector_store=FakeVS(), embedder=FakeEmb())
    assert r("anything", 5, None, None) == ["os", "bh"]   # binds deps, returns ranked ids
