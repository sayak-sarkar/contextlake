"""Tests for wiki generation, the verification council, and the wiki command."""

from argparse import Namespace
from datetime import date

import contextlake.kb.llm as llm_pkg
from contextlake.kb.commands import cmd_wiki
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.council import _parse_review, council_gate, verdict
from contextlake.kb.wiki.generate import generate_page, render_prompt, repo_brief


def _shard(store_dir):
    nodes = [
        Node(id="svc", repo="r", kind="class", name="OrderService", file="svc.py"),
        Node(id="charge", repo="r", kind="function", name="charge", file="svc.py"),
        Node(id="pkg", repo="(packages)", kind="package", name="requests"),
    ]
    edges = [Edge(src="svc", dst="charge", relation="calls", confidence=Confidence.EXTRACTED,
                  provenance=Provenance(source_file="svc.py", source_line=1,
                                        verified_at=date(2026, 6, 21)))]
    write_shard(store_dir, GraphShard(repo="r", head_commit="abc123", nodes=nodes, edges=edges))


class _FakeLlm:
    name = "fake"

    def __init__(self, score=0.9):
        self._score = score

    def generate(self, prompt, *, system=None):
        if "Review lens" in prompt:
            return f'{{"score": {self._score}, "issues": []}}'
        return "## Overview\nOrderService charges orders.\n"


# --- generation -----------------------------------------------------------

def test_repo_brief_and_prompt(tmp_path):
    _shard(tmp_path)
    brief = repo_brief(tmp_path, "r")
    assert brief["head"] == "abc123" and brief["node_count"] == 3
    assert "requests" in brief["packages"]
    prompt = render_prompt(brief)
    assert "OrderService" in prompt and "svc.py" in prompt and "requests" in prompt


def test_generate_page_has_title_body_provenance(tmp_path):
    _shard(tmp_path)
    page = generate_page(_FakeLlm(), tmp_path, "r", verified_at=date(2026, 6, 21))
    assert page.startswith("# r\n")
    assert "OrderService charges orders." in page
    assert "commit `abc123`" in page and "2026-06-21" in page and "`svc.py`" in page


def test_generate_page_none_without_shard(tmp_path):
    assert generate_page(_FakeLlm(), tmp_path, "absent") is None


# --- council --------------------------------------------------------------

def test_parse_review_tolerant():
    assert _parse_review('{"score": 0.8, "issues": ["x"]}') == {"score": 0.8, "issues": ["x"]}
    assert _parse_review("noise {\"score\": 2, \"issues\": []} tail")["score"] == 1.0  # clamped
    assert _parse_review("not json")["score"] == 0.0


def test_verdict_threshold():
    hi = [{"lens": "a", "score": 0.9, "issues": []}, {"lens": "b", "score": 0.8, "issues": []}]
    assert verdict(hi, accept_score=0.7)["accepted"] is True
    lo = [{"lens": "a", "score": 0.4, "issues": ["weak"]}]
    v = verdict(lo, accept_score=0.7)
    assert v["accepted"] is False and "a: weak" in v["issues"]


def test_council_gate_with_fake_llm():
    gate = council_gate(_FakeLlm(score=0.95), "draft", "facts", accept_score=0.7)
    assert gate["accepted"] is True and gate["score"] >= 0.9


# --- command --------------------------------------------------------------

_CFG = '[kb]\nstore_dir = "{store}"\n\n[llm]\nenabled = true\nprovider = "ollama"\n'


def _setup_repo(tmp_path):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(_CFG.format(store=store_dir.as_posix()))
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    store.close()
    _shard(store_dir)
    return store_dir


def test_cmd_wiki_writes_accepted_page(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup_repo(tmp_path)
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: _FakeLlm(score=0.95))

    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    page = store_dir / "wiki" / "r.md"
    assert page.exists() and "OrderService charges orders." in page.read_text()


def test_cmd_wiki_rejects_low_score(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup_repo(tmp_path)
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: _FakeLlm(score=0.2))

    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert not (store_dir / "wiki" / "r.md").exists()  # council rejected it
