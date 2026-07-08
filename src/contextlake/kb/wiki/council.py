"""LLM-council verification gate for generated wiki pages.

Several reviewers each critique the draft through a distinct lens (accuracy,
completeness, clarity) and return a score + issues; a chairman step (deterministic
aggregation) averages the scores and accepts only above a threshold. Keeping the
chairman in code makes the verdict reproducible and testable.
"""

from __future__ import annotations

import json
import re

LENSES = [
    ("accuracy", "Does every statement follow from the provided source facts? "
                 "Flag any claim that looks invented or unsupported."),
    ("completeness", "Does the page cover the repo's main components and "
                     "dependencies without major gaps?"),
    ("clarity", "Is it concise, well-structured, and useful to an engineer new "
                "to the repo?"),
]

REVIEW_SYSTEM = (
    "You are a meticulous, anonymous reviewer. Respond with ONLY a compact JSON "
    'object: {"score": <float 0..1>, "issues": [<short strings>]}.'
)


# Fallback keys a small model might use instead of the requested "score".
_ALT_SCORE_KEYS = ("rating", "overall", "overall_score", "quality")

# A score explicitly labeled "score"/"rating" in prose, e.g. "Score: 0.7 - thin.".
# Requires an explicit separator (":", "=", "is", "of") between the keyword and the
# number so an unrelated small integer near the word -- e.g. an issue-list ordinal
# like "rating 1 - the intro lacks context" -- is never mistaken for a labeled score.
_LABELED_SCORE_RE = re.compile(
    r"(?i)\b(?:score|rating)\b\s*(?:[:=]|\bis\b)\s*([01](?:\.\d+)?)\b")

# An "N/10" or "N out of 10" style rating, e.g. "I'd rate this 8/10.".
_FRACTION_10_RE = re.compile(r"(?i)\b([0-9](?:\.\d+)?)\s*(?:/|out of)\s*10\b")


def _extract_score(obj, text: str) -> float | None:
    # Recovery ladder for when the requested "score" key is missing, tried in order
    # and stopping at the first hit. Deliberately conservative: it never picks up a
    # bare, unlabeled number from prose (e.g. one inside an issue description) --
    # only a value under a plausible alternate key, or a number explicitly labeled
    # as a score/rating, or the common "N/10" shorthand.
    #
    # If the JSON object parsed successfully, ITS score lives in a key -- try the
    # alternate keys only. Scanning the raw text (which includes the issue/description
    # strings the model itself wrote) is what causes fabrication: a keyword like
    # "rating" inside an issue string can sit near an unrelated small integer (e.g.
    # an ordinal, "rating 1 - ..."). Only fall back to scanning prose when the JSON
    # was totally unparseable, i.e. the score (if any) can only live in free text.
    if isinstance(obj, dict):
        for key in _ALT_SCORE_KEYS:
            val = obj.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                val = float(val)
                if 0.0 <= val <= 1.0:
                    return val
        return None
    match = _LABELED_SCORE_RE.search(text)
    if match:
        val = float(match.group(1))
        if 0.0 <= val <= 1.0:
            return val
    match = _FRACTION_10_RE.search(text)
    if match:
        val = float(match.group(1)) / 10.0
        if 0.0 <= val <= 1.0:
            return val
    return None


def _parse_review(text: str) -> dict:
    # A review abstains (parsed=False) — rather than scoring 0 — whenever we can't
    # extract a usable numeric score, whether the JSON was malformed OR valid-but-in
    # the wrong shape (small local models do both). One flaky review must not sink an
    # otherwise well-reviewed page; its issues are still surfaced.
    try:
        obj = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        obj = None

    raw = obj.get("score") if isinstance(obj, dict) else None
    try:
        score, scored = max(0.0, min(1.0, float(raw))), True
    except (TypeError, ValueError):
        score, scored = 0.0, False   # no usable "score" yet -> try the recovery ladder

    if not scored:
        fallback = _extract_score(obj, text)
        if fallback is not None:
            score, scored = max(0.0, min(1.0, fallback)), True

    issues = [str(i) for i in (obj.get("issues") or [])][:10] if isinstance(obj, dict) else []
    if scored:
        return {"score": score, "issues": issues, "parsed": True}
    if not issues:
        issues = ["unparseable review"] if obj is None else ["review had no parseable score"]
    return {"score": 0.0, "issues": issues, "parsed": False}


def review(llm, draft: str, facts: str, *, lenses=LENSES) -> list[dict]:
    reviews = []
    for key, ask in lenses:
        prompt = (f"Source facts:\n{facts}\n\nDraft wiki page:\n{draft}\n\n"
                  f"Review lens — {key}: {ask}")
        reviews.append({"lens": key, **_parse_review(llm.generate(prompt, system=REVIEW_SYSTEM))})
    return reviews


def verdict(reviews: list[dict], *, accept_score: float = 0.7) -> dict:
    """Chairman: average the *parseable* reviewer scores and decide accept/reject.

    Reviews that failed to parse (``parsed=False``) abstain — they are excluded from
    the mean rather than counted as zero, so a single malformed review from a small
    local model doesn't reject an otherwise well-reviewed page. If *no* review parsed,
    the page can't be verified and is rejected.
    """
    issues = [f"{r['lens']}: {i}" for r in reviews for i in r["issues"]]
    scored = [r for r in reviews if r.get("parsed", True)]
    if not scored:
        return {"accepted": False, "score": 0.0,
                "issues": issues or ["no reviews"], "abstained": len(reviews)}
    score = sum(r["score"] for r in scored) / len(scored)
    return {"accepted": score >= accept_score, "score": round(score, 3),
            "issues": issues, "abstained": len(reviews) - len(scored)}


def council_gate(llm, draft: str, facts: str, *, accept_score: float = 0.7,
                 council_size: int | None = None, lenses=LENSES) -> dict:
    # council_size (from [llm]) trims how many review lenses run; None/0 = all of them.
    if council_size:
        lenses = lenses[:max(1, council_size)]
    return verdict(review(llm, draft, facts, lenses=lenses), accept_score=accept_score)
