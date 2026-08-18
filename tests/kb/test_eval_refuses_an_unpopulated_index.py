"""`kb eval --retriever semantic` must refuse an empty index, not score it zero.

A retriever over an empty vector table returns nothing for every query, so the run
completes and reports P@k=0, R@k=0, hit-rate=0 with exit 0: a confident measurement of
a search that never ran. `kb eval --json` exists to gate CI on exactly these numbers,
so a zero here reads as "retrieval regressed to nothing" and blocks a release.

`kb query` degrades to full-text in the same situation, and that difference is
deliberate: a query still has a useful answer to give, while an eval's entire output is
the score of the retriever that was asked for.
"""

from __future__ import annotations

import json

import pytest

from contextlake.cli import main


def _setup(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
                   '\n[embeddings]\nenabled = true\nprovider = "builtin"\n')
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"queries": [
        {"query": "charge_card", "expected": ["svc-billing::charge_card"]}]}))
    repo = tmp_path / "svc-billing"
    repo.mkdir()
    (repo / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    return cfg, golden, repo


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def test_scoring_semantic_over_an_empty_index_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, golden, repo = _setup(tmp_path)
    assert _run(["kb", "index", str(repo), "--config", str(cfg)]) == 0
    capsys.readouterr()

    code = _run(["kb", "eval", "--golden", str(golden), "--retriever", "semantic",
                 "--config", str(cfg)])
    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert text.strip(), "the capture must see something or this proves nothing"
    assert code == 1, "a score that was never measured must not exit 0"
    assert "kb embed" in text, "the message must carry the remedy"
    for absent in ("P@k=", "hit-rate="):
        assert absent not in text, "no metric may be printed for a search that never ran"


def test_the_json_form_names_the_condition_rather_than_reporting_zeroes(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, golden, repo = _setup(tmp_path)
    assert _run(["kb", "index", str(repo), "--config", str(cfg)]) == 0
    capsys.readouterr()

    code = _run(["kb", "eval", "--golden", str(golden), "--retriever", "semantic",
                 "--json", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert out.strip(), "the capture must see something or this proves nothing"
    payload = json.loads(out)
    assert code == 1
    assert payload["error"] == "index_unpopulated"
    assert "hit_rate" not in payload


def test_the_fts_retriever_is_unaffected(tmp_path, monkeypatch, capsys):
    """The guard is about the vector path. Keyword scoring reads no vectors and must
    keep working on the same store, or the refusal has quietly disabled the default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, golden, repo = _setup(tmp_path)
    assert _run(["kb", "index", str(repo), "--config", str(cfg)]) == 0
    capsys.readouterr()

    code = _run(["kb", "eval", "--golden", str(golden), "--json", "--config", str(cfg)])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["retriever"] == "fts"
    assert payload["n"] == 1
