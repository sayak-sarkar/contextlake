"""Deterministic structural validation of a generated wiki draft.

Runs *before* the LLM council (``council.council_gate``) on every draft, whatever
produced it. The council is a judge, and a judge is only as good as the model
behind it: a controlled run with the shipped default provider accepted every page
it was shown, including a draft that was one sentence repeated 32 times (scored
0.967, the highest of that run) and one whose "Gotchas" section was the prompt's
own guardrail sentence reproduced word for word. Swapping in a larger judge moved
those verdicts but did not make them reliable.

Both of those failures are mechanically detectable with no model and no network
call, which is what this module does. It is a floor, not a replacement: it says
nothing about whether a well-formed page is *true*, which is still the council's
job. Everything here is cheap enough to run per page: a handful of linear passes
over the draft's tokens, no model and no network.
"""

from __future__ import annotations

import re
from collections import Counter

# Words, normalized: lowercase alphanumeric runs, punctuation and markdown
# dropped. Matching on tokens rather than raw text means a model that reflowed
# an instruction, swapped its quote characters, or re-wrapped its lines is still
# caught, and it keeps the repetition counter from treating "the `kb` module"
# and "the kb module" as different sentences.
_WORD_RE = re.compile(r"[a-z0-9]+")

# How many consecutive words a draft must share with an instruction before that
# counts as the model echoing the instruction rather than following it.
# Measured on the 8 pages of the controlled run (tests/kb/fixtures/wiki/): the
# pages a human judged sound share no run of even 6 words with the instruction
# text, while the page that leaked its Gotchas instruction shares a run of 61.
# 12 sits clear of both ends. It is deliberately not lower: a compliant page is
# *asked* to talk about the same subject as the instruction, so short shared
# phrasing ("in the graph and is therefore worth extra care") is normal writing.
_LEAK_RUN_WORDS = 12

# How many times one sentence (or one 12-word span, for prose that never
# punctuates) may repeat before the draft is a degenerate loop rather than
# writing. Measured on the same 8 pages: the highest repeat count in a page
# judged sound is 4 (a section preamble restated once per section), while the
# two degenerate pages repeat a single sentence 31 and 32 times. 8 is twice the
# legitimate ceiling and under a third of the observed failure floor.
_MAX_REPEATS = 8

# Sentences shorter than this are not counted: headings, list labels and stock
# fragments ("See below.") legitimately recur, and they carry too little content
# for repetition to mean anything. Set to the same width as the leak run so both
# rules share one notion of "a span long enough to be evidence".
_MIN_SENTENCE_WORDS = 12


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _spans(words: list[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]


def _sentences(draft: str) -> list[str]:
    """Normalized sentences, one per clause of every non-blank line.

    Splits per line first so a markdown list -- where each item is its own
    sentence but the line break, not a full stop, ends it -- counts as many
    sentences rather than one long one. That shape is exactly how one of the
    degenerate pages repeats itself.
    """
    out = []
    for line in draft.splitlines():
        line = line.strip().lstrip("#-*>+ ").strip()
        for part in re.split(r"(?<=[.!?])\s+", line):
            words = _words(part)
            if len(words) >= _MIN_SENTENCE_WORDS:
                out.append(" ".join(words))
    return out


def leaked_instruction(draft: str, instructions) -> str | None:
    """The instruction span a draft reproduced verbatim, or None.

    ``instructions`` is the prompt's own directive prose (``generate.
    PROMPT_INSTRUCTIONS`` / ``cluster.CLUSTER_PROMPT_INSTRUCTIONS``), never the
    facts interpolated around it -- a good page repeats the repo's file and
    symbol names by design, so matching against the whole rendered prompt would
    reject exactly the pages that did their job.
    """
    draft_spans = set(_spans(_words(draft), _LEAK_RUN_WORDS))
    if not draft_spans:
        return None
    for instruction in instructions:
        for span in _spans(_words(instruction), _LEAK_RUN_WORDS):
            if span in draft_spans:
                return " ".join(span)
    return None


def repeated_span(draft: str) -> tuple[str, int] | None:
    """The (text, count) of a span repeated past ``_MAX_REPEATS``, or None.

    Sentence repeats are reported in preference to raw word-span repeats because
    a whole sentence is what a reader recognizes in the rejection message, but
    both are checked: a loop that never emits a full stop still repeats its
    words, and the run that motivated this module ended mid-word.

    The word-span pass stays *within* a line. Spanning line breaks would count
    the seam between two short lines as a repeat, so a page with twenty
    identically shaped rows ("## Dependencies" / "- pytest" / "- ruff") would be
    rejected for the one thing markdown does most: repeat structure.
    """
    for text, count in Counter(_sentences(draft)).most_common(1):
        if count > _MAX_REPEATS:
            return text, count
    spans: Counter = Counter()
    for line in draft.splitlines():
        spans.update(_spans(_words(line), _LEAK_RUN_WORDS))
    for span, count in spans.most_common(1):
        if count > _MAX_REPEATS:
            return " ".join(span), count
    return None


def structural_gate(draft: str, instructions=()) -> dict | None:
    """None when the draft is structurally sound, else a rejection verdict.

    The verdict is shaped like ``council.verdict``'s (``accepted``/``score``/
    ``issues``) so a caller gates on it the same way, plus a ``reason`` naming
    the rule that fired -- rejecting a page without saying which defect it hit
    leaves an operator staring at a missing file, which is the failure mode this
    whole module exists to end.
    """
    leak = leaked_instruction(draft, instructions)
    if leak is not None:
        return {
            "accepted": False, "score": 0.0, "reason": "prompt leakage",
            "issues": [f"reproduces its own prompt instruction verbatim: \"{leak}...\""],
        }
    repeat = repeated_span(draft)
    if repeat is not None:
        text, count = repeat
        return {
            "accepted": False, "score": 0.0, "reason": "degenerate repetition",
            "issues": [f"repeats one span {count} times: \"{text[:80]}...\""],
        }
    return None
