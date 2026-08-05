"""Query-shape router for the ``ask`` tool — pick the right substrate for a
natural-language question, deterministically and offline.

A small-context IDE agent shouldn't have to know which of twenty tools to call.
``classify(question)`` maps a question to one of a handful of *routes* (each backed
by an existing graph/wiki/search substrate) plus the *target* symbol or repo the
question is about. Pure, stdlib-only, and unit-/eval-tested so the routing is
falsifiable — a misroute shows up as a number dropping on the golden set.

The graph stays the spine: every route but ``explain`` returns cited graph facts;
``explain`` returns advisory wiki prose, clearly labeled.
"""

from __future__ import annotations

import re

# Routes, each backed by a substrate in server.py:
#   definition -> find_definition      callers    -> find_callers
#   dependents -> find_dependents      impact     -> blast_radius
#   owners     -> who_knows            explain    -> get_wiki (advisory)
#   search     -> semantic_search / search_code (the NL fallback)
DEFINITION = "definition"
CALLERS = "callers"
DEPENDENTS = "dependents"
SUBCLASSES = "subclasses"
IMPACT = "impact"
OWNERS = "owners"
EXPLAIN = "explain"
SEARCH = "search"

# Ordered (most specific first). The first pattern that matches wins, so a
# change-verb question routes to impact before the bare "depends on" would send
# it to dependents. Each pattern is matched case-insensitively against the whole
# question.
_RULES: list[tuple[str, re.Pattern]] = [
    (IMPACT, re.compile(
        r"\b(blast\s*radius|what\s+breaks|breaks?\s+if|impact\s+of|affected\s+by|"
        r"safe\s+to\s+(change|remove|delete|rename)|if\s+i\s+(change|remove|delete|"
        r"rename|modify)|ripple)\b", re.I)),
    (OWNERS, re.compile(
        r"\b(who\s+(owns|knows|wrote|maintains|should\s+i\s+ask)|owner\s+of|"
        r"maintainer|\bsme\b|subject\s+matter|expert\s+(on|in|for))\b", re.I)),
    (SUBCLASSES, re.compile(
        r"\b(subclasses?\s+of|subtypes?\s+of|what\s+(extends|implements|subclasses)|"
        r"who\s+(extends|implements)|classes?\s+that\s+(extend|implement)|"
        r"implementations?\s+of|derived\s+(from|classes)|extended\s+by)\b", re.I)),
    (DEPENDENTS, re.compile(
        r"\b(what\s+depends\s+on|who\s+depends\s+on|depends?\s+on|dependents?\s+of|"
        r"which\s+repos?\s+(use|depend)|reverse\s+depend|consumers?\s+of\s+the\s+"
        r"package)\b", re.I)),
    (CALLERS, re.compile(
        r"\b(who\s+calls|what\s+calls|callers?\s+of|called\s+by|call\s+sites?|"
        r"usages?\s+of|who\s+uses|where\s+is\s+\S+\s+used)\b", re.I)),
    (DEFINITION, re.compile(
        r"\b(where\s+is\s+\S+\s+defined|where(?:'s|\s+is)\b|defined|definition\s+of|"
        r"declar(?:e|ed|ation)|find\s+the\s+definition|locate)\b", re.I)),
    (EXPLAIN, re.compile(
        r"\b(explain|how\s+does\b|how\s+do(?:es)?\s+\S+\s+work|what\s+is\b|what'?s\b|"
        r"overview\s+of|describe|tell\s+me\s+about|architecture\s+of|walk\s+me\s+"
        r"through|summar(?:y|ize|ise))\b", re.I)),
]

# Words that are never the target symbol (question scaffolding).
_STOP = frozenset(
    "a an the is are was were be to of in on for with and or not it this that "
    "where who what how why which does do did defined definition calls call "
    "caller callers used uses use depends depend dependents owns owner knows "
    "know wrote maintainer maintains explain describe about work works breaks "
    "break if i change remove delete rename modify impact blast radius safe "
    "package repo repository function class method module symbol code you me "
    "please can could would should extends extend implements implement subclass "
    "subclasses subtype subtypes derived implementations".split())

# A code-ish token: dotted path, snake_case, CamelCase, hyphenated package, or a
# bare identifier — anything that reads like a symbol/repo rather than prose.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")
_QUOTED = re.compile(r"[`\"']([^`\"']+)[`\"']")


def _looks_like_symbol(tok: str) -> bool:
    if tok.lower() in _STOP or len(tok) < 2:
        return False
    return bool(
        "." in tok or "_" in tok or "-" in tok or "/" in tok
        or re.search(r"[a-z][A-Z]", tok)          # camelCase
        or tok[0].isupper()                        # ClassName
        or (tok.islower() and tok not in _STOP and len(tok) >= 3
            and re.search(r"[a-z]", tok))          # a plausible lowercase identifier
    )


def extract_target(question: str) -> str | None:
    """The symbol / repo id the question is about, or None. A backticked or quoted
    span wins; otherwise the most symbol-like identifier (last one, since questions
    tend to put the subject at the end: 'who calls charge_order')."""
    m = _QUOTED.search(question)
    if m:
        return m.group(1).strip()
    # Strip trailing punctuation that _IDENT swallowed: it accepts '.', '-' and '/'
    # so a dotted path survives, which also makes a sentence-final word plus its
    # full stop ("line.") a valid-looking symbol. Strip first, then judge, so the
    # stripped word is ranked as the prose it is rather than as a dotted path.
    candidates = [s for t in _IDENT.findall(question)
                  if (s := t.rstrip("./-_")) and _looks_like_symbol(s)]
    if not candidates:
        return None
    # Prefer clearly-code tokens (dotted/underscored/camel/hyphen) over bare words;
    # among equals, the last one (the subject usually trails the question).
    strong = [t for t in candidates if any(c in t for c in "._-/")
              or re.search(r"[a-z][A-Z]", t) or t[0].isupper()]
    return (strong or candidates)[-1]


# A sentence break: a terminator followed by whitespace, or the end of the string.
# Deliberately requires the whitespace so a dotted identifier ("Foo.bar", "api.v2")
# is never split in half.
_SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+")


def _asking_sentence(question: str, pattern: re.Pattern) -> str:
    """The sentence that triggered the route, or the whole question.

    ``extract_target`` takes the last symbol-like token, which is right for a
    single-clause question and wrong the moment an ordinary second sentence
    follows: "Who calls classify? List every caller." put the route's real
    subject in sentence one and the last token in sentence two, so the product
    answered a question nobody asked. The route was decided by a pattern that
    matched in one specific sentence; the subject is in that sentence.
    """
    parts = [s for s in _SENTENCE_SPLIT.split(question.strip()) if s.strip()]
    if len(parts) < 2:
        return question
    for part in parts:
        if pattern.search(part) and extract_target(part):
            return part
    return question


def classify(question: str) -> tuple[str, str | None]:
    """Map a question to ``(route, target)``. Unmatched questions fall back to
    SEARCH (semantic/FTS), which is always safe."""
    q = question.strip()
    for route, pattern in _RULES:
        if pattern.search(q):
            return route, extract_target(_asking_sentence(q, pattern))
    return SEARCH, extract_target(q)


# Generic English scaffolding, plus the route verbs and the meta-nouns a person
# uses to describe the KIND of thing they want ("the payments *service*", "which
# *repository*"). Deliberately NOT _STOP, which also drops domain words like
# impact / blast / radius / package because they are route keywords -- those are
# exactly the words that anchor a question like "blast radius impact analysis",
# and dropping them there left no term to probe at all.
_GENERIC = frozenset("""
a an the is are was were be been being am do does did doing have has had having
to of in on at for with and or not it its this that these those there here
what which who whom whose when where why how can could would should may might
will shall must me my we our you your they their he she him her his hers
about into over under from by as if then than so such all any both each few
more most other some only own same too very just also i no nor but
give list show tell find get make use used using name explain describe
summarise summarize summary overview walk through work works working
defined define definition declared declaration call calls called caller callers
depend depends dependent dependents own owns owner owners know knows wrote
maintain maintains maintainer extend extends implement implements
repository repo repos service services component components system
file files line lines code function class method module package symbol
main every please thanks
""".split())

_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def content_terms(question: str) -> list[str]:
    """The words in ``question`` worth probing the index for, first occurrence
    first, case preserved.

    Used to answer "does this knowledge base contain anything the question is
    about?" -- a question made entirely of terms the graph has never heard of
    cannot be answered from the graph, however confidently a nearest-neighbour
    search ranks its top k.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TERM.findall(question):
        low = tok.lower()
        if len(tok) < 3 or low in _GENERIC or low in seen:
            continue
        seen.add(low)
        out.append(tok)
    return out
