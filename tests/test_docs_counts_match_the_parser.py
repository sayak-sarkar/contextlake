"""A count the docs state about the command surface must match the parser.

`docs/cli-reference.md` said "34 commands: 6 top-level, 8 under `mirror`, 20 under `kb`" beside
tables that list 35 (6, 8 and 21). `docs/code-graph-model.md` said "three tiers" above a table
with four rows. Both numbers were typed by hand, so both drifted when a row was added.

The authority for the command counts is the parser itself. The authority for the tier count is
the table the sentence introduces: a sentence that disagrees with its own table is a defect.
The authority for the lint command a contributor is told to run is the one CI runs (`ci.yml`).

This file needs no `[kb]` extra: `build_parser()` is core.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from contextlake.cli import _ALIASES, _NAMESPACES, build_parser

REPO = Path(__file__).resolve().parents[1]

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}


def _as_int(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _choices(parser: argparse.ArgumentParser) -> dict:
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices


def _command_counts() -> tuple[int, int, int]:
    """(top-level, under mirror, under kb), leaf commands only, aliases not counted."""
    parser = build_parser()
    top = [n for n in _choices(parser) if n not in _NAMESPACES and n not in _ALIASES]
    per_namespace = {
        ns: [n for n in _choices(parser._namespace_parsers[ns]) if n not in _ALIASES]
        for ns in _NAMESPACES}
    return len(top), len(per_namespace["mirror"]), len(per_namespace["kb"])


def _kb_index_help(flag: str) -> str:
    index = _choices(build_parser()._namespace_parsers["kb"])["index"]
    return next(a.help for a in index._actions if flag in a.option_strings)


def test_kb_index_repo_help_and_docs_name_the_fallback_id() -> None:
    """A git repo with no `origin` is filed as `name@<root commit>`, not as its folder name."""
    assert "root commit" in _kb_index_help("--repo")
    docs = re.sub(r"\s+", " ", _text("docs/indexing-the-code-graph.md"))
    assert "the id falls back to the directory name" not in docs
    assert "name@<first 12 characters of its root commit>" in docs


def test_kb_index_repos_help_says_the_filter_is_anchored() -> None:
    """`--repos pp` selects nothing named `app`; the filter is a whole-string glob match."""
    from contextlake.core import match_repo_filter

    help_ = _kb_index_help("--repos")
    assert "anchored" in help_ and "glob/substring" not in help_
    assert not match_repo_filter("acme/app", "app", ["pp"])
    assert match_repo_filter("acme/app", "app", ["*pp*"])


def test_internals_lists_every_index_the_schema_creates() -> None:
    """The page said "Five indexes" while the schema creates six (`ix_edges_repo` was added for
    `repo_counts` and `clear_repo`). The schema is the authority for the count and the names."""
    _SCHEMA = pytest.importorskip("contextlake.kb.store.sqlite_store")._SCHEMA

    made = re.findall(r"CREATE INDEX IF NOT EXISTS \w+ ON (\w+)\((\w+)\)", _SCHEMA)
    text = re.sub(r"\s+", " ", _text("docs/internals.md"))
    m = re.search(r"\b(\w+) indexes: `", text)
    assert m, "docs/internals.md no longer states 'N indexes: `table.column`, ...'"
    assert _as_int(m.group(1)) == len(made), (
        f"the page says {m.group(1)}; the schema has {len(made)}")
    for table, column in made:
        assert f"`{table}.{column}`" in text, f"internals.md does not list {table}.{column}"


def test_the_stated_command_counts_match_the_parser() -> None:
    m = re.search(r"^(\d+) commands: (\d+) top-level, (\d+) under `mirror`, (\d+) under `kb`",
                  _text("docs/cli-reference.md"), re.M)
    assert m, "docs/cli-reference.md no longer states 'N commands: A top-level, B under ...'"
    total, top, mirror, kb = (int(g) for g in m.groups())
    assert (top, mirror, kb) == _command_counts()
    assert total == top + mirror + kb, "the stated total is not the sum of the stated parts"


def test_each_reference_table_lists_every_command_in_its_tier() -> None:
    text = _text("docs/cli-reference.md")
    top, mirror, kb = _command_counts()
    def rows(heading: str, pattern: str) -> list[str]:
        section = text.split(heading, 1)[1].split("\n###", 1)[0]
        return re.findall(pattern, section, re.M)

    rows_top = rows("### Top-level commands", r"^\| `([a-z-]+)` \|")
    rows_mirror = rows("### Mirror commands", r"^\| `mirror ([a-z-]+)` \|")
    rows_kb = rows("### Knowledge-layer commands", r"^\| `kb ([a-z-]+)` \|")
    assert (len(rows_top), len(rows_mirror), len(rows_kb)) == (top, mirror, kb)


def test_the_stated_tier_count_matches_the_tier_table() -> None:
    text = _text("docs/code-graph-model.md")
    m = re.search(r"Depth differs across (\w+) tiers", text)
    assert m, "docs/code-graph-model.md no longer states how many depth tiers there are"
    table = text.split("| Tier | Languages | What you get |", 1)[1].split("\n\n", 1)[0]
    rows = [ln for ln in table.splitlines() if ln.startswith("| ") and not ln.startswith("| ---")]
    assert _as_int(m.group(1)) == len(rows)


_LINT = re.compile(r"(?:ruff|\$\(RUFF\)) check(?: --fix)? "
                   r"((?:src|tests|scripts|benchmarks)(?: (?:src|tests|scripts|benchmarks))*)")


def test_the_lint_command_a_contributor_runs_is_the_one_ci_runs() -> None:
    """CONTRIBUTING.md, the Makefile, the add-a-language recipe and the PR checklist said
    `ruff check src tests` while both CI lint steps run `src tests scripts benchmarks`. A
    contributor who edited `scripts/` or `benchmarks/` got a green locally and a red CI.
    docs/releasing.md quotes the old command on purpose, as history, so it is not checked."""
    ci = re.findall(r"run: ruff check ([\w ]+)$", _text(".github/workflows/ci.yml"), re.M)
    assert ci and len(set(ci)) == 1, f"ci.yml no longer runs one lint command: {ci}"
    for rel in ("CONTRIBUTING.md", "Makefile", "docs/adding-a-language.md",
                ".github/pull_request_template.md"):
        found = _LINT.findall(_text(rel))
        assert found, f"{rel} no longer shows the lint command"
        for paths in found:
            assert paths == ci[0], f"{rel} lints '{paths}'; CI lints '{ci[0]}'"
