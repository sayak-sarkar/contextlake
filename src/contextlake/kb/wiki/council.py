"""LLM-council verification gate for generated wiki pages.

Several reviewers each critique the draft through a distinct lens (accuracy,
completeness, clarity) and return a score + issues; a chairman step (deterministic
aggregation) averages the scores and accepts only above a threshold. Keeping the
chairman in code makes the verdict reproducible and testable.
"""

from __future__ import annotations

import json

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


def _parse_review(text: str) -> dict:
    try:
        obj = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        # A review we couldn't parse abstains (parsed=False) rather than scoring 0 —
        # small local models often return malformed JSON, and one flaky review should
        # not sink an otherwise well-reviewed page.
        return {"score": 0.0, "issues": ["unparseable review"], "parsed": False}
    try:
        score = max(0.0, min(1.0, float(obj.get("score", 0.0))))
    except (TypeError, ValueError):
        score = 0.0
    issues = obj.get("issues") or []
    return {"score": score, "issues": [str(i) for i in issues][:10], "parsed": True}


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
