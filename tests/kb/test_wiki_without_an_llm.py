"""`kb wiki` produces a real page with no LLM configured.

Before this, the command printed "LLM tier disabled" and wrote nothing, so a user who had
not set up a backend got no wiki at all from a tool whose identity is local-first. The
structural page needs no model, so it is written on every run, and the generated page is
what an LLM adds ON TOP.

Driven through `cmd_wiki` rather than by calling the stage directly, because the property
under test is entirely about the command's control flow: the early return happened BEFORE
any page was written, and a test that called the stage would pass for the whole time that
return was in the way.
"""

from __future__ import annotations

import logging
from argparse import Namespace
from datetime import date

from contextlake.kb.cmds.wiki import cmd_wiki
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

# No [llm] table at all: the state a user is in before they configure anything.
_CFG_NO_LLM = '[kb]\nstore_dir = "{store}"\n'
_CFG_WITH_LLM = '[kb]\nstore_dir = "{store}"\n\n[llm]\nenabled = true\nprovider = "ollama"\n'


def _setup(tmp_path, cfg=_CFG_NO_LLM):
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(cfg.format(store=store_dir.as_posix()),
                                      encoding="utf-8")
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    store.close()
    nodes = [
        Node(id="svc", repo="r", kind="class", name="CatalogService", file="svc.py"),
        Node(id="main", repo="r", kind="entry_point", name="main", file="cmd/main.go"),
        Node(id="charge", repo="r", kind="function", name="charge", file="svc.py"),
    ]
    edges = [Edge(src="charge", dst="svc", relation="calls",
                  confidence=Confidence.EXTRACTED,
                  provenance=Provenance(source_file="svc.py", source_line=1,
                                        verified_at=date(2026, 6, 21)))]
    write_shard(store_dir, GraphShard(repo="r", head_commit="abc123",
                                      nodes=nodes, edges=edges))
    return store_dir


def _page(store_dir):
    """The CANONICAL wiki path. A repository has one wiki page per scope, and the
    structural page IS that page until something verified replaces it."""
    return store_dir / "wiki" / "r.md"


def test_a_page_is_written_with_no_llm_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    page = _page(store_dir)
    assert page.exists(), "no structural page was written without an LLM configured"
    text = page.read_text(encoding="utf-8")
    assert text.startswith("# r")
    assert "`main`" in text, "the entry point did not reach the page"


def test_the_run_says_the_structural_pages_are_complete_on_their_own(tmp_path,
                                                                    monkeypatch, caplog):
    """The message is as much the deliverable here as the file is.

    "LLM tier disabled" told a user their run had failed to do anything. It now does
    something, and the line has to say so, or somebody deletes the store and tries again.

    `caplog` on the project's own logger, not `capsys`: this command logs, so stdout is
    empty and a capsys assertion passes or fails for reasons unrelated to the message.
    The non-empty check below is there because a capture that sees nothing makes every
    `in` assertion after it vacuous.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path)
    with caplog.at_level(logging.INFO, logger="contextlake"):
        assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    out = caplog.text
    assert out.strip(), "the capture saw nothing, so the assertions below prove nothing"
    assert "structural page" in out, out
    assert "need no model" in out, out


def test_the_exit_code_stays_zero(tmp_path, monkeypatch):
    """It was already 0 for "no LLM", and it must stay 0 now that the run does real work:
    a scheduled `kb wiki` on a local-first store is a success, not a skipped step."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0


def test_the_structural_page_is_the_repository_s_wiki_page(tmp_path, monkeypatch):
    """At the canonical path, so every existing reader finds it with no changes: the
    dashboard's Wiki tab, the MCP server and the freshness check all already look here."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert _page(store_dir).exists()
    assert not (store_dir / "wiki" / "_structure").exists(), (
        "the parallel structure directory is gone; one repository has one wiki page")


def test_a_generated_page_is_never_overwritten_by_the_structural_stage(tmp_path,
                                                                      monkeypatch):
    """The load-bearing half of "one wiki page per repository".

    The structural stage runs on EVERY `kb wiki`. Without the kind check it would replace
    an accepted, reviewed prose page with tables on every run, so a scheduled refresh
    would silently undo the generation it was refreshing.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert "no language model" in _page(store_dir).read_text(encoding="utf-8")

    prose = "# r\n\nCatalogService charges orders, reviewed and accepted.\n"
    _page(store_dir).write_text(prose, encoding="utf-8")
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert _page(store_dir).read_text(encoding="utf-8") == prose, (
        "the structural stage overwrote a generated page")


def test_a_structural_page_IS_replaced_on_a_later_run(tmp_path, monkeypatch):
    """The other side of the same pair. Refusing to overwrite anything at all would let a
    structural page go stale forever, and this assertion is what tells the guard above
    apart from a stage that simply never writes twice."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    _page(store_dir).write_text(
        "# r\n\nstale\n\n---\n\n*Built from the knowledge graph with no language "
        "model. Old.*\n", encoding="utf-8")
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert "stale" not in _page(store_dir).read_text(encoding="utf-8")


# --- the invariant, over every reader ------------------------------------------------


def test_no_reader_treats_a_structural_page_as_a_generated_one():
    """The class, not the instances.

    Both page kinds now live at one path, so every reader that used to take "a file is
    here" as "a generated page is here" is wrong in the same way. Three were, and each was
    found separately: the freshness check skipped generation as already-fresh, the module
    selector saw every module as already-paged and stopped rotating onto new ones, and four
    tests asserted absence to mean rejection.

    Parametrising over the readers turns the NEXT one into a failure instead of a fourth
    quiet instance. It reads source rather than exercising behaviour on purpose: a reader
    that never consults the page kind cannot be caught by any input, only by looking.
    """
    import inspect

    from contextlake.kb.cmds import wiki as wiki_cmd

    src = inspect.getsource(wiki_cmd)
    # Each entry: the function that reads a page path, and why it must know the kind.
    readers = {
        "_write_if_not_generated": "it must not overwrite accepted prose",
        "_select_module_pages": "a structural page must count as never-generated",
        "_run_page": "a structural page must not freshness-skip generation",
    }
    for name, why in readers.items():
        fn_src = src.split(f"def {name}(", 1)
        assert len(fn_src) == 2, f"{name} was renamed; this invariant now checks nothing"
        body = fn_src[1].split("\ndef ", 1)[0]
        # Import lines stripped first, and a CALL required, not a mention. The first
        # draft asserted `"is_structural_page" in body`, which the function's own import
        # line satisfies -- so removing the actual call from the selector failed nothing
        # here, and only a separate behavioural test caught it. A guard that passes a
        # break it exists to catch is decorative.
        body = "\n".join(ln for ln in body.splitlines()
                          if "import is_structural_page" not in ln)
        assert "is_structural_page(" in body, (
            f"{name} reads a wiki page path without asking which KIND of page it is, and "
            f"{why}. Both kinds live at the same path, so file presence alone is not the "
            f"question any of these readers mean to ask.")


# --- the structural page grounds the generated one ------------------------------------


class _PromptCapturingLlm:
    name = "capture"

    def __init__(self):
        self.page_prompts: list[str] = []

    def generate(self, prompt, *, system=None):
        if "Review lens" in prompt:
            return '{"score": 0.97, "issues": []}'
        self.page_prompts.append(prompt)
        return ("## Overview\nCatalogService charges orders and `main` starts the "
                "service.\n")


def test_the_generated_prompt_carries_the_structural_page(tmp_path, monkeypatch):
    """The change that attacks the original rejection at its cause.

    An earlier generated wiki was rejected for thin grounding, and the reason was
    structural: the model saw a bounded sample of symbols and wrote confidently about a
    whole repository. It is handed the structural document now, so it writes prose over
    stated facts instead of inferring them from a sample.
    """
    from contextlake.kb import llm as llm_pkg

    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path, _CFG_WITH_LLM)
    cap = _PromptCapturingLlm()
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: cap)
    monkeypatch.setattr(llm_pkg, "build_review_llm", lambda cfg, llm: cap)
    cmd_wiki(Namespace(config=str(tmp_path / "kb.toml")))

    assert cap.page_prompts, "no page prompt was captured, so this proves nothing"
    prompt = cap.page_prompts[0]
    assert "structural page, built from the graph" in prompt, (
        "the prompt does not carry the structural document; it is still grounded on the "
        f"sampled rows:\n{prompt[:600]}")
    # A fact only the structural page states, in the words it states it in.
    assert "Entry points and how to run it" in prompt
    assert "`main`" in prompt


def test_the_structural_page_replaces_the_sampled_rows_rather_than_joining_them(tmp_path,
                                                                               monkeypatch):
    """Both would restate the same symbols in two formats and spend exactly the context
    that thin grounding was a symptom of."""
    from contextlake.kb import llm as llm_pkg

    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path, _CFG_WITH_LLM)
    cap = _PromptCapturingLlm()
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: cap)
    monkeypatch.setattr(llm_pkg, "build_review_llm", lambda cfg, llm: cap)
    cmd_wiki(Namespace(config=str(tmp_path / "kb.toml")))
    prompt = cap.page_prompts[0]
    assert "(indexed graph)" not in prompt, (
        "the sampled-rows block is still present alongside the structural page")


def test_the_page_handed_to_the_model_is_the_one_this_run_rendered(tmp_path, monkeypatch):
    """Not re-read from disk. Once a generated page exists, the file at that path holds
    prose, so reading it back would feed the model its own last output as though it were
    the graph -- a loop that gets worse every run and looks like grounding."""
    from contextlake.kb import llm as llm_pkg

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path, _CFG_WITH_LLM)
    (store_dir / "wiki").mkdir(parents=True, exist_ok=True)
    (store_dir / "wiki" / "r.md").write_text(
        "# r\n\nPROSE FROM A PREVIOUS RUN.\n", encoding="utf-8")
    cap = _PromptCapturingLlm()
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: cap)
    monkeypatch.setattr(llm_pkg, "build_review_llm", lambda cfg, llm: cap)
    cmd_wiki(Namespace(config=str(tmp_path / "kb.toml")))
    assert cap.page_prompts
    assert "PROSE FROM A PREVIOUS RUN" not in cap.page_prompts[0]


def test_the_gotchas_instruction_survives_the_deduplication(tmp_path, monkeypatch):
    """The hubs BLOCK is dropped when the structural page carries the same counts; the
    instruction attached to it must not be.

    That instruction forbids the model from characterising WHY a symbol has many callers
    ("foundational", "critical infrastructure") and requires it to state the count alone.
    Dropping it alongside the block is how a page starts calling a symbol core
    infrastructure on the strength of arithmetic, which is the exact wording defect this
    project already fixed once.
    """
    from contextlake.kb import llm as llm_pkg
    from contextlake.kb.wiki.generate import _GOTCHAS_INSTRUCTION

    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path, _CFG_WITH_LLM)
    cap = _PromptCapturingLlm()
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: cap)
    monkeypatch.setattr(llm_pkg, "build_review_llm", lambda cfg, llm: cap)
    cmd_wiki(Namespace(config=str(tmp_path / "kb.toml")))
    assert cap.page_prompts
    assert _GOTCHAS_INSTRUCTION in cap.page_prompts[0], (
        "the caller-count wording instruction was dropped with the block it sat under")


# --- the page is searchable ------------------------------------------------------------


def test_the_structural_page_is_stored_as_the_repository_s_one_wiki_partition(tmp_path,
                                                                             monkeypatch):
    """Since it is the page everybody gets with no LLM configured, leaving it out of the
    graph would make the DEFAULT wiki the one you cannot search.

    ONE key, the same `@wiki:<repo_id>` a generated page uses. A repository has one wiki, so
    it has one searchable wiki record; separate keys would return two hits about one
    repository, sometimes saying much the same thing.
    """
    from contextlake.kb.store.sqlite_store import SqliteStore

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        first = store.get_node("@wiki:r:0")
        assert first is not None, "the structural page was not stored as a wiki partition"
        # And no second, parallel key for the same repository.
        assert store.get_node("@wiki:r::structure:0") is None
    finally:
        store.close()


def _partition_sections(store_dir, key="@wiki:r") -> list[str]:
    """The section titles a stored wiki partition holds.

    The section NAMES, not a text attribute: the node carries `name`/`file` and the body
    goes to the embeddings rather than onto the node. Asserting on `attrs["text"]` was the
    first draft and it compared None to None, which is a test that cannot fail.
    """
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        out, i = [], 0
        while (n := store.get_node(f"{key}:{i}")) is not None:
            out.append(n.name)
            i += 1
        return out
    finally:
        store.close()


def test_the_partition_is_not_rewritten_when_the_page_was_left_alone(tmp_path, monkeypatch):
    """The file and the partition must never disagree about which page is current.

    The structural stage leaves an accepted prose page alone; if it stored its own partition
    anyway, search would answer from the structural text while the file on disk held prose.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = _setup(tmp_path)
    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    structural_sections = _partition_sections(store_dir)
    assert len(structural_sections) > 1, (
        f"the structural partition has too few sections to tell apart from prose: "
        f"{structural_sections}")

    # Put the store in the state a successful generation leaves: the file holds prose AND
    # its partition holds that prose. One section, so the two are distinguishable.
    from contextlake.kb.cmds.wiki import _store_wiki_partition
    from contextlake.kb.store.sqlite_store import SqliteStore

    prose = "# r\n\n## Summary\n\nAccepted prose, no marker.\n"
    _page(store_dir).write_text(prose, encoding="utf-8")
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        _store_wiki_partition(store, store_dir, "r", prose, "r.md", "abc123")
    finally:
        store.close()
    before = _partition_sections(store_dir)
    assert before != structural_sections, (
        "the prose partition looks identical to the structural one, so the assertion "
        "below could not detect a rewrite")

    assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
    assert _partition_sections(store_dir) == before, (
        "the structural stage re-stored a partition for a page it did not write")
