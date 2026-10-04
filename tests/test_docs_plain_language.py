"""Guard: the mechanical rules of the style guide hold in every hand-written doc.

`docs/style-guide.md` said a lint config "encodes the mechanical rules (banned hype words,
"click here", filler words, em-dashes, "allows you to")". No such config existed: the
only guard was `test_no_emdash_in_docs.py`. The 2026-10-03 audit (O02) found the rules
unguarded. This test is that gate. The em-dash rule stays in its own test.

What it checks, all from `docs/style-guide-voice.md`:

- intensifiers ("very", "really", "simply", ...), plus `much`/`far` before a comparative,
  `pretty` as an adverb, and `exactly` as emphasis ("exactly why");
- downtoners ("somewhat", "fairly", "rather" but not "rather than" or "would rather", ...);
- filler: `just` or `simply` before an instruction verb, "easy"/"easily", and the AI filler
  phrases ("it is worth noting", "in essence", ...);
- hype words, anthropomorphism ("allows you to"), "please", and "click here" link text.

What it leaves out, on purpose. A word with a legitimate sense is only banned in its other
sense, and a bare word match cannot tell the two apart. A `\\bjust\\b` rule once flagged 45
correct sentences meaning "only". So:

- `only`, `even`, `significantly`, `critically`: fine in their literal sense.
- `usually`, `typically`, `often`: the voice guide asks for these over false absolutes.
- `roughly`: banned only "when the number is known", which a regex cannot see.
- `kind of`: "kind" is a domain word here (a node's kind), so "the kind of node" is common.

Masked before any rule runs: fenced blocks and `<pre>` (captured output and code), inline
code spans, double-quoted strings (the style guide quotes the words it bans, and a
before/after example quotes the wrong form), table rows (the guide's before/after tables),
and a glossary bullet whose bold label is itself a banned word (`- **just.** Avoid ...`).

A hit that is correct on purpose goes in `_ALLOWED` with a reason. An entry that matches
nothing fails the test, so the list cannot rot.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Same file set as test_no_emdash_in_docs.py, derived from git so a new page is covered
# without anyone remembering to add it. CHANGELOG.md is a historical record: a shipped entry
# is never rewritten.
_EXCLUDED = {"CHANGELOG.md"}

_WORDS = lambda *ws: r"\b(?:" + "|".join(ws) + r")\b"  # noqa: E731

_COMPARATIVE = (r"(?:more|less|better|worse|faster|slower|larger|smaller|greater|fewer|"
                r"higher|lower|easier|harder|cheaper|safer|simpler|too)")

# (name, pattern). All rules match case-insensitively.
RULES: list[tuple[str, str]] = [
    ("intensifier", _WORDS(
        "very", "really", "quite", "extremely", "incredibly", "absolutely", "totally",
        "truly", "genuinely", "deeply", "highly", "thoroughly", "perfectly", "completely",
        "simply", "literally", "actually", "obviously", "clearly", "certainly", "definitely",
        "massively", "hugely", "dramatically", "substantially", "considerably",
        "particularly", "especially", "crucially", "fundamentally", "essentially",
        "basically", "effectively", "indeed")),
    ("intensifier", r"\b(?:much|far)\s+" + _COMPARATIVE + r"\b|\bby far\b|\bso much\b"),
    # "pretty-print" and "pretty print" are a term, not an intensifier.
    ("intensifier", r"\bpretty\b(?![- ]?print)"),
    ("intensifier", r"\bexactly\s+(?:why|this|what|how|that|these|those|the point)\b"),
    ("downtoner", _WORDS(
        "somewhat", "slightly", "fairly", "a bit", "sort of", "relatively", "marginally",
        "largely", "mostly", "broadly", "arguably", "more or less", "to some extent")),
    # "rather than" compares, and "would rather" means "prefer". Neither is a downtoner.
    ("downtoner", r"(?<!would )(?<!'d )\brather\b(?!\s+than)"),
    ("filler", r"\b(?:just|simply)\s+(?:run|add|use|set|do(?! not|n.t)|install|type|call|"
               r"pass|open|edit|write|make|put|drop|point|tell|ask|press|click|restart|"
               r"delete|remove|copy|change|works?|need|check|start|stop)\b"),
    ("filler", r"(?<!-)\b(?:easy|easily)\b(?!-)"),
    ("filler", r"\b(?:it is worth noting|it'?s worth noting|worth noting|"
               r"it is important to (?:note|understand)|in essence|at the end of the day|"
               r"needless to say|suffice it to say|it should be noted)\b"),
    ("hype", _WORDS("leverage", "leverages", "leveraging", "seamless", "seamlessly",
                    "powerful", "revolutionary", "supercharge", "next-gen", "robust",
                    "cutting-edge", "unleash")),
    ("anthropomorphism", r"\b(?:allows you to|lets you|enables you to)\b"),
    ("please", r"\bplease\b"),
    ("bad link text", r"\[(?:click here|here|read this|below|above)\]\("),
]

# A bullet labelled with a banned word (`- **just.**`) or with the name of a rule
# (`- **Hype:** leverage, seamless, ...`) is the guide stating the rule, not breaking it.
_GLOSSARY_LABEL = re.compile(
    "|".join(p for _n, p in RULES)
    + r"|\b(?:hype|intensifiers?|downtoners?|overclaims?|anthropomorphism|filler)\b", re.I)

# Inline spans and quotes may wrap onto the next line of the same paragraph. Matching them
# one line at a time breaks the pairing on the second line: `("exactly 200` + newline +
# `records"), banned as emphasis ("exactly why")` read the closing quote of the first as the
# opening of a new one, and unmasked the banned example.
_SAME_PARAGRAPH = r"(?:[^{q}\n]|\n(?![ \t]*\n))*?"

# (path, a fragment of the line that holds the hit) -> why the word is correct there.
_ALLOWED: dict[tuple[str, str], str] = {
    ("CODE_OF_CONDUCT.md", "investigated promptly and fairly"):
        "fairly means with fairness here, not a downtoner",
    ("BRANDING.md", "slightly taller than wide"):
        "art direction: the degree is the instruction to the illustrator, not a hedge on a fact",
    ("BRANDING.md", "eyes slightly more open"):
        "art direction: the degree is the instruction to the illustrator, not a hedge on a fact",
    ("BRANDING.md", "a slightly bigger smile"):
        "art direction: the degree is the instruction to the illustrator, not a hedge on a fact",
    ("docs/branding/mascot.md", "slightly taller than wide"):
        "art direction: the degree is the instruction to the illustrator, not a hedge on a fact",
}


def _prose_files() -> list[str]:
    try:
        listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.md"],
                                 capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):  # sdist, or no git on PATH
        pytest.skip("not a git checkout, so the tracked-file set cannot be derived")
    out = [rel for rel in listing
           if "tests" not in Path(rel).parts and rel not in _EXCLUDED]
    assert len(out) >= 30, f"only {len(out)} prose files discovered; the listing is wrong"
    return out


def _blank(match: re.Match) -> str:
    """Replace a masked region with its newlines only, so line numbers stay true."""
    return " " + "\n" * match.group(0).count("\n")


def mask(text: str) -> str:
    """Remove everything that is not the page's own prose. Line numbers are preserved."""
    text = re.sub(r"^[ \t]*(```+|~~~+).*?^[ \t]*\1[ \t]*$", _blank, text, flags=re.S | re.M)
    text = re.sub(r"<pre\b.*?</pre>", _blank, text, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", _blank, text, flags=re.S)
    text = re.sub("`" + _SAME_PARAGRAPH.format(q="`") + "`", _blank, text)
    text = re.sub('"' + _SAME_PARAGRAPH.format(q='"') + '"', _blank, text)
    text = re.sub("“" + _SAME_PARAGRAPH.format(q="”") + "”", _blank, text)
    text = re.sub(r"^[ \t]*\|.*$", "", text, flags=re.M)
    # A glossary bullet names the word it rules on: `- **just.** Avoid as a minimizer`.
    # Masked only when the bold label is itself banned, so a content bullet that happens to
    # start with bold text is still checked.
    return re.sub(
        r"^[ \t]*[-*][ \t]+\*\*([^*]+?)[.:]?\*\*[.:]?.*(?:\n(?![ \t]*(?:[-*][ \t]|#|$)).*)*",
        lambda m: _blank(m) if _GLOSSARY_LABEL.search(m.group(1)) else m.group(0),
        text, flags=re.M)


def hits(text: str) -> list[tuple[int, str, str]]:
    """(line number, rule name, matched text) for every rule hit in the page's prose."""
    prose = mask(text)
    found = []
    for name, pattern in RULES:
        for m in re.finditer(pattern, prose, re.I):
            found.append((prose.count("\n", 0, m.start()) + 1, name, m.group(0)))
    return sorted(found)


def _problems() -> tuple[list[str], set[tuple[str, str]]]:
    bad, used = [], set()
    for rel in _prose_files():
        path = REPO / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        for n, name, word in hits(text):
            line = lines[n - 1]
            key = next((k for k in _ALLOWED if k[0] == rel and k[1] in line), None)
            if key:
                used.add(key)
                continue
            bad.append(f"{rel}:{n}: {name} {word!r}: {line.strip()[:100]}")
    return bad, used


def test_docs_follow_the_mechanical_style_rules():
    bad, _used = _problems()
    assert not bad, (
        f"{len(bad)} style-rule hit(s) in documentation prose. Replace an intensifier with "
        "the number or delete it; see docs/style-guide-voice.md. A hit that is correct on "
        "purpose goes in _ALLOWED with a reason.\n" + "\n".join(bad))


def test_every_allowlist_entry_still_matches():
    _bad, used = _problems()
    stale = sorted(set(_ALLOWED) - used)
    assert not stale, f"_ALLOWED entries that match nothing; delete them: {stale}"


@pytest.mark.parametrize("sentence, rule", [
    ("This is very fast.", "intensifier"),
    ("It runs much faster now.", "intensifier"),
    ("That is exactly why it fails.", "intensifier"),
    ("The output is pretty long.", "intensifier"),
    ("It is somewhat slower.", "downtoner"),
    ("The index is rather slow.", "downtoner"),
    ("Just run the installer.", "filler"),
    ("Setup is easy.", "filler"),
    ("It is worth noting that the cache is cold.", "filler"),
    ("A seamless upgrade.", "hype"),
    ("The flag allows you to skip it.", "anthropomorphism"),
    ("Please restart the server.", "please"),
    ("See [here](cli-reference.md).", "bad link text"),
])
def test_each_rule_fires(sentence, rule):
    assert [r for _n, r, _w in hits(sentence)] == [rule]


@pytest.mark.parametrize("sentence", [
    "Use a list rather than a table.",
    "If you would rather not run a local model, use Ollama.",
    "It indexes just the parser module.",
    "The run wrote exactly 200 records.",
    "Pass --pretty-print to indent the JSON.",
    "The graph records the kind of node.",
    "Only the first repo is read, even on 3.10.",
    "The difference is significantly above noise (p < 0.01).",
    "Run `contextlake kb index --really-fast` to see the refusal.",
    'The guide bans "just run X" as a minimizer.',
    "```\nthis is very fast\n```",
    "| very fast | 3x faster |",
    "- **simply.** Avoid, like the minimizer above.",
    "- **Hype:** leverage, seamless, powerful.",
    'Fine for literal identity ("exactly 200\nrecords"), banned as emphasis ("exactly why").',
])
def test_legitimate_uses_pass(sentence):
    assert hits(sentence) == []


def test_masking_keeps_line_numbers():
    text = "first\n```\nvery\n```\nThis is very fast.\n"
    assert hits(text) == [(5, "intensifier", "very")]
