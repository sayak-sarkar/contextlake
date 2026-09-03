"""`kb index` writes the API reference and the design notes, once, with no model.

A freshly indexed store used to hold an empty `docs/` and say nothing about it. The reader
opened the Docs tab, found it empty, and had no reason to think a second command was needed.

Two properties carry this file and neither is provable on its own:

- the documents appear by DEFAULT, and `--no-docs` is the only way to get none. Asserted as
  a pair, because "--no-docs writes nothing" passes on code where nothing writes anything.
- a repo whose head commit has not moved is not rewritten. Asserted by planting a sentinel
  in the page and reading it back byte-exactly, so a clock or an mtime resolution cannot
  decide the result.
- a repo whose GRAPH this run rewrote IS rewritten, whatever its pages say. The commit
  alone is not the key: `--force` and a parser bump both rebuild a graph at an unchanged
  commit, and the pages rendered from the graph that was replaced are then wrong. Each of
  those is asserted as a pair of counts, 0-written against N-written, because "the run
  passed" is true of both.

`kb hook` installs `kb index` on every commit (`kb/git_hook.py:74`), so the skip is the
difference between a per-commit cost that stays flat and one that grows with the repo.

Precondition for every test here: HOME is monkeypatched and the config names a store under
`tmp_path`, so no ambient store or ambient `kb.toml` is read.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from contextlake import cli

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

# `run` calls `helper` on a line of its own so the quoted call site is unambiguous.
SOURCE = "def helper(x):\n    return x + 1\n\n\ndef run():\n    return helper(1)\n"
SECOND = "def helper(x):\n    return x + 1\n\n\ndef alarum():\n    return helper(2)\n"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                   capture_output=True, text=True)


def _repo(path, source=SOURCE, message="c1"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text(source, encoding="utf-8")
    if not (path / ".git").exists():
        _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", message], path)


def _store(tmp_path, name="kb"):
    """A config naming this test's own store, never the user's real one."""
    store_dir = tmp_path / name
    cfg = tmp_path / (name + ".toml")
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n[embeddings]\nenabled = false\n')
    return store_dir, cfg


def _index(cfg, ws, *extra):
    from contextlake.kb.commands import cmd_index

    return cmd_index(cli.build_parser().parse_args(
        ["kb", "index", "--config", str(cfg), "--workspace", str(ws), *extra]))


def _graph_nodes(store_dir):
    """Nodes the store holds, so an assertion about the graph reads the graph."""
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        return store.stats().nodes
    finally:
        store.close()


def _pages(store_dir):
    api = store_dir / "docs" / "api"
    design = store_dir / "docs" / "design"
    return (len(list(api.glob("*.md"))) if api.is_dir() else 0,
            len(list(design.glob("*.md"))) if design.is_dir() else 0)


def _stamp_an_older_parser(store_dir, version="0"):
    """Stand in for a parser bump by moving the store's stamp, not the build's.

    `PARSER_VERSION` cannot be monkeypatched into this: the shard carries its own copy and
    the index compares the store row against the constant the running build imports. Moving
    the row is what a real bump looks like from the index's side, and it is what puts the
    repo back into the work list.
    """
    from contextlake.kb.state import mark_repo_indexed
    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        for repo_id, head in [(r.id, r.head_commit) for r in store.list_repos()]:
            mark_repo_indexed(store, repo_id, head, version)
    finally:
        store.close()


def _plant(page):
    """Append a sentinel below the stamp and return the exact bytes now on disk."""
    marked = page.read_text(encoding="utf-8") + "\n<!-- SENTINEL -->\n"
    page.write_text(marked, encoding="utf-8")
    return marked


def test_a_plain_index_writes_both_documents_and_no_docs_writes_none(tmp_path, monkeypatch):
    """The pair is the test. A lone `--no-docs writes nothing` assertion passes against
    today's code, where a plain index writes nothing either."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")

    on_store, on_cfg = _store(tmp_path, "on")
    assert _index(on_cfg, ws) == 0
    assert _pages(on_store) == (1, 1)

    off_store, off_cfg = _store(tmp_path, "off")
    assert _index(off_cfg, ws, "--no-docs") == 0
    assert _pages(off_store) == (0, 0)

    # Content, not a placeholder: the symbol from the graph and the call-site line read
    # off the working tree.
    body = next((on_store / "docs" / "api").glob("*.md")).read_text(encoding="utf-8")
    assert "`run` *(function)*" in body
    assert "`return helper(1)`" in body


def test_the_index_writes_no_fleet_page(tmp_path, monkeypatch):
    """The whole-store page stays with `kb docs` and `bootstrap`.

    This run's target list is a subset by construction: a repo that failed, went over the
    memory guard or was filtered out by `--repos` is never in it. A page headed "the whole
    store" built from part of one is the false claim `cmd_docs` refuses to make.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "a")
    _repo(ws / "b")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws) == 0
    assert _pages(store_dir) == (2, 2)
    assert not (store_dir / "docs" / "fleet").exists()


def test_reindex_with_no_new_commit_rewrites_nothing_and_says_unchanged(
        tmp_path, monkeypatch, gls_logs):
    """The count must move from 1 to 0, not merely stay green."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws) == 0
    page = next((store_dir / "docs" / "api").glob("*.md"))
    # APPENDED, not written over. The freshness test reads the commit marker the page
    # carries, so replacing the whole file would destroy the stamp and make the second run
    # regenerate for the right reason -- a test that passes on broken code.
    marked = page.read_text(encoding="utf-8") + "\n<!-- SENTINEL -->\n"
    page.write_text(marked, encoding="utf-8")

    gls_logs.clear()
    assert _index(cfg, ws) == 0
    # Byte-exact, so no clock or mtime resolution can decide this.
    assert page.read_text(encoding="utf-8") == marked
    assert "documents already describe" in gls_logs.text
    assert "0 written, 1 unchanged, documents failed for 0 repo(s)" in gls_logs.text


def test_a_moved_head_regenerates_the_document(tmp_path, monkeypatch):
    """A skip that never un-skips is the failure `kb wiki` shipped once, where file
    EXISTENCE was read as "already generated" and `fresh` stayed permanently empty."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws) == 0
    page = next((store_dir / "docs" / "api").glob("*.md"))
    page.write_text(page.read_text(encoding="utf-8") + "\n<!-- SENTINEL -->\n",
                    encoding="utf-8")

    _repo(ws / "solo", source=SECOND, message="c2")
    assert _index(cfg, ws) == 0
    body = page.read_text(encoding="utf-8")
    assert "SENTINEL" not in body
    assert "API reference" in body
    assert "alarum" in body


def test_a_document_failure_leaves_the_index_exit_code_intact_and_is_reported(
        tmp_path, monkeypatch, gls_logs):
    """rc == 0 on its own is vacuous. The written graph and the named count carry it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    from contextlake.kb.docs import api as docs_api

    def _boom(*a, **k):
        raise RuntimeError("renderer tripped")

    # Resolved at call time by the function-local import in generate_docs.
    monkeypatch.setattr(docs_api, "render_api_reference", _boom)

    assert _index(cfg, ws) == 0
    assert "documents failed for 1 repo(s)" in gls_logs.text
    assert "renderer tripped" in gls_logs.text
    assert _pages(store_dir) == (0, 0)
    # The graph is written and intact, which is what rc == 0 is a verdict about.
    assert _graph_nodes(store_dir) == 3


def test_the_index_never_builds_a_model_for_documents(tmp_path, monkeypatch):
    """The hot path stays model-free even when `--llm` is on the command line.

    `--llm` is accepted BEFORE the subcommand (`cli.py:764`), so
    `contextlake --llm anthropic kb index` reaches `cmd_index` with `args.llm` set, and
    `kb hook` runs `kb index` after every commit.

    Two guards, because a model can enter by two doors. `build_llm` is the door `cmd_docs`
    uses. `render_orientation` is the only thing `generate_docs` does with a model at all,
    so it trips on ANY truthy llm handed through, including a raw provider string that
    never went near `build_llm`.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    from contextlake.kb import llm as kb_llm
    from contextlake.kb.docs import draft as docs_draft

    def _never(*a, **k):
        raise AssertionError("the index put a model on the per-commit path")

    monkeypatch.setattr(kb_llm, "build_llm", _never)
    monkeypatch.setattr(docs_draft, "render_orientation", _never)

    args = cli.build_parser().parse_args(
        ["kb", "index", "--config", str(cfg), "--workspace", str(ws)])
    args.llm = "anthropic"
    args.llm_model = "x"
    from contextlake.kb.commands import cmd_index

    assert cmd_index(args) == 0
    # Written, not merely "no exception": generate_docs catches a per-repo failure, so an
    # AssertionError from either guard shows up as zero pages.
    assert _pages(store_dir) == (1, 1)
    body = next((store_dir / "docs" / "api").glob("*.md")).read_text(encoding="utf-8")
    # The orientation block sits ABOVE the reference; a model-free page starts at the
    # heading.
    assert body.lstrip().startswith("# ")


@pytest.mark.parametrize("flag", ["--no-docs"])
def test_no_docs_leaves_the_graph_alone(tmp_path, monkeypatch, flag):
    """The flag must switch off the documents and nothing else."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws, flag) == 0
    assert _pages(store_dir) == (0, 0)
    assert _graph_nodes(store_dir) == 3, "the flag switched off the graph too"


def test_force_rewrites_a_document_the_stamp_calls_current(tmp_path, monkeypatch, gls_logs):
    """`--force` means redo it, and the documents are part of what this command does.

    The pair of counts is the test. `--force` rebuilds every graph, so a run that leaves
    every page alone has re-parsed the code and then reported pages rendered from the graph
    it replaced as current. 0-written is asserted first on a plain re-run, so the 1-written
    that follows is the flag moving the number and not the skip being absent.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws) == 0
    page = next((store_dir / "docs" / "api").glob("*.md"))
    marked = _plant(page)

    gls_logs.clear()
    assert _index(cfg, ws) == 0
    assert page.read_text(encoding="utf-8") == marked
    assert "0 written, 1 unchanged, documents failed for 0 repo(s)" in gls_logs.text

    gls_logs.clear()
    assert _index(cfg, ws, "--force") == 0
    assert "1 written, 0 unchanged, documents failed for 0 repo(s)" in gls_logs.text
    body = page.read_text(encoding="utf-8")
    assert "SENTINEL" not in body
    assert "API reference" in body


def test_a_parser_bump_regenerates_the_documents_it_re_indexed(
        tmp_path, monkeypatch, gls_logs):
    """The run that rebuilds a graph must not report the old pages as current.

    A parser change makes a different graph out of the same code, so the head commit alone
    is the wrong key. The index says so in its own log line ("the graph they hold is not
    the one this build produces") and that same run used to leave the documents rendered
    from the replaced graph in place, counted as unchanged.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws) == 0
    page = next((store_dir / "docs" / "api").glob("*.md"))
    marked = _plant(page)

    # The control: the same run with the stamp left alone rewrites nothing.
    gls_logs.clear()
    assert _index(cfg, ws) == 0
    assert page.read_text(encoding="utf-8") == marked
    assert "0 written, 1 unchanged, documents failed for 0 repo(s)" in gls_logs.text

    _stamp_an_older_parser(store_dir)
    gls_logs.clear()
    assert _index(cfg, ws) == 0
    # The graph half fired, so the documents half is being asked the right question.
    assert "were built by an older parser" in gls_logs.text
    assert "1 written, 0 unchanged, documents failed for 0 repo(s)" in gls_logs.text
    assert "SENTINEL" not in page.read_text(encoding="utf-8")


@pytest.mark.parametrize("field,pattern,replacement", [
    ("repo", r"repo=\S+ ", "repo=other/elsewhere "),
    ("kind", r"kind=\S+ ", "kind=design "),
])
def test_a_marker_naming_another_document_is_not_read_as_this_page(
        tmp_path, monkeypatch, gls_logs, field, pattern, replacement):
    """A commit read out of the marker on its own proves less than it looks.

    Two repositories sitting at one commit make one repository's page a valid-looking stamp
    for the other, so a page copied or moved onto the wrong path is read as current and
    never rewritten. The marker carries `kind` and `repo` beside the commit; the skip has
    to read all three. The marker is edited here rather than staged with two repos at one
    commit, so the field under test is the only thing that differs.
    """
    import re

    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "solo")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws) == 0
    page = next((store_dir / "docs" / "api").glob("*.md"))
    marked = _plant(page)

    gls_logs.clear()
    assert _index(cfg, ws) == 0
    assert page.read_text(encoding="utf-8") == marked
    assert "0 written, 1 unchanged, documents failed for 0 repo(s)" in gls_logs.text

    swapped = re.sub(pattern, replacement, marked, count=1)
    assert swapped != marked, f"the {field} field was not found in the marker"
    page.write_text(swapped, encoding="utf-8")

    gls_logs.clear()
    assert _index(cfg, ws) == 0
    assert "1 written, 0 unchanged, documents failed for 0 repo(s)" in gls_logs.text
    body = page.read_text(encoding="utf-8")
    assert "SENTINEL" not in body
    assert replacement.strip() not in body, "the foreign marker survived the rewrite"


def test_only_the_repo_whose_graph_was_rebuilt_is_regenerated(tmp_path, monkeypatch, gls_logs):
    """The rebuild set is per repo, not a switch for the whole run.

    One repo commits and the other does not, so one goes through `_persist` and the other
    through the unchanged branch. A set that collected every id, or a term that stopped
    reading the set at all, gives "2 written, 0 unchanged" here and passes every
    single-repo row in this file. The sentinel says which of the two pages was left alone.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    _repo(ws / "a")
    _repo(ws / "b")
    store_dir, cfg = _store(tmp_path)

    assert _index(cfg, ws) == 0
    api = store_dir / "docs" / "api"
    # A repo with no origin remote is filed as `<dir>@<root-commit>`, so the page is found
    # by its prefix rather than named. Asserted, so a rename cannot silently pick one page
    # twice and leave the comparison reading the same file against itself.
    pages = {d: next(api.glob(d + "@*.md")) for d in ("a", "b")}
    assert pages["a"] != pages["b"]
    still = _plant(pages["b"])
    moves = _plant(pages["a"])

    _repo(ws / "a", source=SECOND, message="c2")
    gls_logs.clear()
    assert _index(cfg, ws) == 0
    assert "1 written, 1 unchanged, documents failed for 0 repo(s)" in gls_logs.text
    assert pages["b"].read_text(encoding="utf-8") == still
    moved = pages["a"].read_text(encoding="utf-8")
    assert moved != moves
    assert "alarum" in moved
