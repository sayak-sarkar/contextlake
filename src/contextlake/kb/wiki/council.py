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
        return {"score": 0.0, "issues": ["unparseable review"]}
    try:
        score = max(0.0, min(1.0, float(obj.get("score", 0.0))))
    except (TypeError, ValueError):
        score = 0.0
    issues = obj.get("issues") or []
    return {"score": score, "issues": [str(i) for i in issues][:10]}


def review(llm, draft: str, facts: str, *, lenses=LENSES) -> list[dict]:
    reviews = []
    for key, ask in lenses:
        prompt = (f"Source facts:\n{facts}\n\nDraft wiki page:\n{draft}\n\n"
                  f"Review lens — {key}: {ask}")
        reviews.append({"lens": key, **_parse_review(llm.generate(prompt, system=REVIEW_SYSTEM))})
    return reviews


def verdict(reviews: list[dict], *, accept_score: float = 0.7) -> dict:
    """Chairman: average the reviewer scores, collect issues, decide accept/reject."""
    if not reviews:
        return {"accepted": False, "score": 0.0, "issues": ["no reviews"]}
    score = sum(r["score"] for r in reviews) / len(reviews)
    issues = [f"{r['lens']}: {i}" for r in reviews for i in r["issues"]]
    return {"accepted": score >= accept_score, "score": round(score, 3), "issues": issues}


def council_gate(llm, draft: str, facts: str, *, accept_score: float = 0.7, lenses=LENSES) -> dict:
    return verdict(review(llm, draft, facts, lenses=lenses), accept_score=accept_score)
