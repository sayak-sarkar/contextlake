"""An OPTIONAL model-drafted orientation section for `kb docs`.

`kb docs` is deliberately model-free: its whole claim is that every line traces to the graph,
so a reader can check any of it. That default does not change here. This module adds an opt-in
section, requested with `--llm`, and it is bound by three rules that keep the claim intact:

1. **Additive, never a replacement.** The deterministic document is rendered first and passed in
   whole. The model writes an orientation paragraph ABOVE it; nothing generated can edit,
   reorder, or drop a fact the graph produced.
2. **Grounded in what the page already says.** The prompt carries the rendered document, not the
   raw graph, so the model has nothing to invent from. Asked to summarise a page it can see, its
   failure mode is a bland summary rather than a fabricated symbol.
3. **Marked, always.** The section is fenced by a marker naming the provider and model. A reader
   who wants the model-free document can strip it mechanically, and `kb docs` without `--llm`
   never writes it at all.

The alternative, letting a model draft the reference itself, was rejected: `kb docs` and
`kb wiki` are different products on purpose. The wiki is council-verified prose ABOUT a repo;
the reference is a mechanical record OF it. Blending them costs the reference the one property
that makes it worth reading.
"""
from __future__ import annotations

BEGIN = "<!-- contextlake:draft BEGIN provider={provider} model={model} -->"
END = "<!-- contextlake:draft END -->"

_PROMPT = """You are writing a short orientation for a generated API reference.

The document below was produced mechanically from a code graph. Every symbol, file path, line
number and call-site count in it is real. Your job is ONLY to help a reader decide where to
start.

Rules, and they are strict:
- Use ONLY what appears in the document. Do not name a symbol, file, or number that is not in it.
- Do not restate the counts. The reader can see them.
- Do not evaluate the code, praise it, or speculate about intent.
- 3 to 5 sentences. No headings, no lists, no preamble.
- If the document is too thin to orient anyone, say exactly that in one sentence.

DOCUMENT:
{document}
"""


def render_orientation(llm, document: str, *, repo_id: str) -> str | None:
    """The marked orientation block, or None when the model declines or fails.

    Returns None rather than raising: a documentation run that already produced a correct
    deterministic page must not fail because an optional prose tier was unavailable.
    """
    if llm is None or not document.strip():
        return None
    try:
        body = (llm.generate(_PROMPT.format(document=document[:12000])) or "").strip()
    except Exception as exc:
        # SAY why. A silent None here means the reader asked for --llm, got a document with no
        # orientation, and no reason -- and the usual cause is an actionable missing extra whose
        # own error text carries the fix. Swallowing that is the failure mode this project has a
        # rule against.
        from ...logging_setup import log
        log(f"  orientation skipped for {repo_id}: {str(exc).splitlines()[0]}")
        return None
    if not body:
        return None
    provider = getattr(llm, "provider", None) or getattr(llm, "name", "unknown")
    model = getattr(llm, "model", None) or "unknown"
    return "\n".join([
        BEGIN.format(provider=provider, model=model),
        f"> **Orientation, written by a model** ({provider} · {model}). Everything below the "
        f"marker is generated mechanically from the graph and is checkable; this paragraph is "
        f"not. It summarises the page and adds no facts of its own.",
        "",
        body,
        "",
        END,
        "",
    ])


def strip_draft(text: str) -> str:
    """Remove a draft block, so the model-free document can always be recovered."""
    start = text.find("<!-- contextlake:draft BEGIN")
    if start == -1:
        return text
    end = text.find(END, start)
    if end == -1:
        return text
    return text[:start] + text[end + len(END):].lstrip("\n")
