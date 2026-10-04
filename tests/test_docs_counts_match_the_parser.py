"""A count the docs state about the command surface must match the parser.

`docs/cli-reference.md` said "34 commands: 6 top-level, 8 under `mirror`, 20 under `kb`" beside
tables that list 35 (6, 8 and 21). `docs/code-graph-model.md` said "three tiers" above a table
with four rows. Both numbers were typed by hand, so both drifted when a row was added.

The authority for the command counts is the parser itself. The authority for the tier count is
the table the sentence introduces: a sentence that disagrees with its own table is a defect.

This file needs no `[kb]` extra: `build_parser()` is core.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

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
