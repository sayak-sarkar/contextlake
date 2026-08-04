"""Tests for the deterministic pre-council validation of a generated wiki draft.

The fixtures under ``fixtures/wiki/`` are the eight pages from a controlled run
of the real command (five from the small bundled model, three from a local
ollama model), kept byte for byte as they were generated. They are the reason
this module exists: the council accepted every one of them, including a page
that repeats one sentence 32 times and a page whose Gotchas section is its own
prompt instruction. They must stay unsanitized -- editing the defect out of a
fixture would leave the test passing and the guard unproven.
"""

from argparse import Namespace
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

import contextlake.kb.llm as llm_pkg
from contextlake.kb.commands import cmd_wiki
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.cluster import CLUSTER_PROMPT_INSTRUCTIONS, render_cluster_prompt
from contextlake.kb.wiki.generate import PROMPT_INSTRUCTIONS, render_prompt, repo_brief
from contextlake.kb.wiki.validate import (
    _LEAK_RUN_WORDS,
    _MAX_REPEATS,
    _sentences,
    leaked_instruction,
    repeated_span,
    structural_gate,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wiki"

_CFG = '[kb]\nstore_dir = "{store}"\n\n[llm]\nenabled = true\nprovider = "ollama"\n'


class _FakeLlm:
    """Scores every review lens the same and drafts one sound sentence."""

    name = "fake"

    def __init__(self, score=0.95):
        self._score = score

    def generate(self, prompt, *, system=None):
        if "Review lens" in prompt:
            return f'{{"score": {self._score}, "issues": []}}'
        return "## Overview\nCatalogService charges orders.\n"


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


def _shard(store_dir):
    nodes = [
        Node(id="svc", repo="r", kind="class", name="CatalogService", file="svc.py"),
        Node(id="charge", repo="r", kind="function", name="charge", file="svc.py"),
        Node(id="pkg", repo="(packages)", kind="package", name="requests"),
    ]
    edges = [Edge(src="svc", dst="charge", relation="calls", confidence=Confidence.EXTRACTED,
                  provenance=Provenance(source_file="svc.py", source_line=1,
                                        verified_at=date(2026, 6, 21)))]
    write_shard(store_dir, GraphShard(repo="r", head_commit="abc123", nodes=nodes, edges=edges))

# The accept/reject table for the whole controlled run, measured not guessed.
# `judged-contextlake` is the same page re-reviewed by a larger judge model,
# which rejected the repetition page but still accepted a page recommending
# `pip install python` -- kept here because the structural verdict must not
# depend on which judge saw the page.
EVIDENCE = [
    ("wiki-llm-builtin-contextlake.md", "prompt leakage"),
    ("wiki-llm-builtin-judged-contextlake.md", "prompt leakage"),
    ("wiki-llm-builtin-module-site.md", None),
    ("wiki-llm-builtin-module-src.md", "degenerate repetition"),
    ("wiki-llm-builtin-module-tests.md", "degenerate repetition"),
    ("wiki-llm-ollama-contextlake.md", None),
    ("wiki-llm-ollama-module-src.md", None),
    ("wiki-llm-ollama-module-tests.md", None),
]


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name,reason", EVIDENCE, ids=[n for n, _ in EVIDENCE])
def test_evidence_pages_get_their_measured_verdict(name, reason):
    gate = structural_gate(_page(name), PROMPT_INSTRUCTIONS)
    if reason is None:
        assert gate is None, f"{name} should pass, got {gate}"
    else:
        assert gate is not None, f"{name} should be rejected for {reason}"
        assert gate["reason"] == reason
        assert gate["accepted"] is False and gate["score"] == 0.0
        assert gate["issues"], "a rejection must say what it saw"


def test_the_three_human_approved_pages_pass():
    """Named explicitly, not just as table rows: these are the pages a human read
    and judged sound, so they are the ones a false positive would cost."""
    for name in ("wiki-llm-ollama-contextlake.md", "wiki-llm-ollama-module-src.md",
                 "wiki-llm-ollama-module-tests.md"):
        assert structural_gate(_page(name), PROMPT_INSTRUCTIONS) is None, name


def test_repetition_margin_over_the_soundest_page():
    """The accepted page that repeats itself the most must clear the limit by a
    real margin, not scrape past it -- if a prompt change pushes legitimate
    repetition up, this fails before the guard starts eating good pages."""
    worst = 0
    for name, reason in EVIDENCE:
        if reason is not None:
            continue
        # Count the top span directly rather than through the > limit check.
        counts = Counter(_sentences(_page(name)))
        worst = max(worst, max(counts.values(), default=0))
    assert worst <= _MAX_REPEATS // 2, (
        f"a sound page repeats a sentence {worst} times, too close to the "
        f"limit of {_MAX_REPEATS}")


def test_leak_detection_survives_a_reworded_instruction():
    """The point of matching against the live instruction text: reword the
    prompt and the guard follows it, with no second copy to update."""
    reworded = ("Whenever you write Gotchas, say nothing beyond the caller count "
                "each symbol already has, and never guess at why it is high.")
    echoing = "## Gotchas\n" + reworded + "\n"
    assert leaked_instruction(echoing, [reworded]) is not None
    # ...and the old wording is no longer what is being looked for.
    assert leaked_instruction(echoing, PROMPT_INSTRUCTIONS) is None


def test_a_compliant_page_about_the_same_subject_passes():
    """The instructions and a good page necessarily talk about the same things.
    A page that follows the Gotchas directive, names its subsystems, and says it
    is grounded in the facts must not be mistaken for one that copied them."""
    page = (
        "# repo\n\n"
        "## Overview\nA service that resolves catalog identifiers.\n\n"
        "## Architecture\nThe repo has three subsystems, each with its own page: "
        "`api`, `worker`, `docs`. The api subsystem exposes the HTTP surface, the "
        "worker subsystem drains the queue, and docs holds the published guides.\n\n"
        "## Gotchas\n"
        "- `close` in `store/base.py` has 283 callers in the graph, so changes "
        "there deserve extra care and tests.\n"
        "- `log` in `logging_setup.py` has 85 callers in the graph and warrants "
        "the same caution.\n"
    )
    assert structural_gate(page, PROMPT_INSTRUCTIONS) is None


def test_repetition_threshold_boundary():
    line = ("The catalog service resolves every product identifier against the "
            "nightly pricing index before the batch runs.\n")
    assert repeated_span(line * _MAX_REPEATS) is None
    hit = repeated_span(line * (_MAX_REPEATS + 1))
    assert hit is not None and hit[1] == _MAX_REPEATS + 1


def test_repetition_caught_without_sentence_punctuation():
    """A loop that never emits a full stop (the run that motivated this ended
    mid-word) still repeats its words, so the word-span rule backs the
    sentence rule up."""
    run_on = "the kb module contains the server function of the kb module and " * 20
    assert repeated_span(run_on) is not None


def test_short_repeated_lines_are_not_repetition():
    """Headings, table rows and one-line labels recur by design."""
    page = "# repo\n\n" + "## Dependencies\n- pytest\n- ruff\n" * 20
    assert repeated_span(page) is None


def test_leak_needs_a_long_run_not_a_shared_phrase():
    """A handful of shared words is how two texts about one subject read, not
    evidence of copying."""
    instruction = PROMPT_INSTRUCTIONS[0]
    words = instruction.split()
    short = " ".join(words[:_LEAK_RUN_WORDS - 2])
    assert leaked_instruction(f"## Overview\n{short}\n", PROMPT_INSTRUCTIONS) is None
    long = " ".join(words[:_LEAK_RUN_WORDS + 4])
    assert leaked_instruction(f"## Overview\n{long}\n", PROMPT_INSTRUCTIONS) is not None


def test_facts_in_the_prompt_are_not_part_of_the_instruction_corpus(tmp_path):
    """Guards the split that makes this workable: a good page quotes the repo's
    own symbols and file names, all of which appear in the rendered prompt. Only
    the directive prose may be matched against."""
    _shard(tmp_path)
    prompt = render_prompt(repo_brief(tmp_path, "r"))
    for instruction in PROMPT_INSTRUCTIONS:
        assert "CatalogService" not in instruction and "svc.py" not in instruction
    # every instruction is genuinely part of the prompt, or the guard would be
    # matching against text no model was ever shown
    assert PROMPT_INSTRUCTIONS[-1] in prompt


def test_cluster_instructions_are_part_of_the_cluster_prompt():
    brief = {"namespace": "ns", "repos": [], "member_count": 0,
             "internal_edges": [], "boundary_edges": [], "heads": {}, "truncated": False}
    prompt = render_cluster_prompt(brief)
    for instruction in CLUSTER_PROMPT_INSTRUCTIONS:
        assert instruction in prompt


def test_empty_and_tiny_drafts_do_not_crash():
    assert structural_gate("", PROMPT_INSTRUCTIONS) is None
    assert structural_gate("# repo\n", PROMPT_INSTRUCTIONS) is None


# --- wiring ---------------------------------------------------------------

def test_cmd_wiki_rejects_a_degenerate_page_before_the_council(tmp_path, monkeypatch,
                                                               gls_logs):
    """End to end: a looping draft is never written, the operator is told which
    defect dropped it, and the council is never consulted about it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup_repo(tmp_path)
    reviewed = []

    class _LoopingLlm(_FakeLlm):
        def generate(self, prompt, *, system=None):
            if "Review lens" in prompt:
                reviewed.append(prompt)
                return '{"score": 0.97, "issues": []}'
            return "The kb module contains the server function of the kb module. " * 20

    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: _LoopingLlm())
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert not (store_dir / "wiki" / "r.md").exists()
    assert not reviewed, "a structurally broken page must not reach the council"
    text = gls_logs.text
    assert "degenerate repetition" in text
    assert "0 written, 1 rejected" in text


def test_cmd_wiki_rejects_a_prompt_echoing_page(tmp_path, monkeypatch, gls_logs):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup_repo(tmp_path)

    class _EchoingLlm(_FakeLlm):
        def generate(self, prompt, *, system=None):
            if "Review lens" in prompt:
                return '{"score": 0.97, "issues": []}'
            return "## Overview\nA service.\n\n## Gotchas\n" + PROMPT_INSTRUCTIONS[2]

    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: _EchoingLlm())
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert not (store_dir / "wiki" / "r.md").exists()
    assert "prompt leakage" in gls_logs.text


def test_cmd_wiki_still_writes_a_sound_page(tmp_path, monkeypatch):
    """The guard must not stand between a good draft and disk."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup_repo(tmp_path)
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: _FakeLlm(score=0.95))
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert (store_dir / "wiki" / "r.md").exists()
