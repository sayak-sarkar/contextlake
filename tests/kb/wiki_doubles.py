"""A shared "sound draft" for the wiki test doubles.

The replacement gate requires generated prose to be at least as complete as the structural
page it would displace: every section that page rendered must be addressed. A one-line
fixture draft is not, and correctly gets rejected.

Every wiki test that wants to exercise the WRITE path therefore needs a draft that clears
that bar. Building it from the prompt is what makes it honest rather than a bypass: the
draft mentions the names the structural page actually stated, so it passes for the same
reason a real page would, and a draft that stopped covering the page would still fail.

One module rather than a copy per test file, because these two things must agree: the gate's
notion of "complete" and the fixture's notion of "sound". Two copies would drift, and the
drift would surface as a test suite that passes while the gate does nothing.
"""

from __future__ import annotations

import re

# The instruction spans a draft must never echo -- `structural_gate` rejects a page that
# reproduces its own prompt, so a fixture built FROM the prompt has to avoid quoting them.
_INSTRUCTION_HINTS = ("Review lens", "Do not", "Never", "You are", "untrusted")


def backticked_names(prompt: str) -> list[str]:
    """Every backticked span in ``prompt``, in first-seen order, deduplicated.

    Order is kept stable so a draft built from it is deterministic: a test that fails
    intermittently because a set iterated differently is worse than no test.
    """
    seen, out = set(), []
    for m in re.findall(r"`([^`\n]{1,120})`", prompt or ""):
        name = m.strip()
        if name and name not in seen and not any(h in name for h in _INSTRUCTION_HINTS):
            seen.add(name)
            out.append(name)
    return out


def sound_draft(prompt: str) -> str:
    """Prose that clears every gate, derived from the structural page inside ``prompt``.

    Reads the page's own `## <section>` headings straight out of the prompt rather than
    first slicing the untrusted block that wraps it. Slicing was the first approach and it
    was fragile in the way that matters: getting the block's END boundary slightly wrong
    silently changed which sections the draft covered, so the fixture failed the
    completeness gate for a reason that had nothing to do with the code under test. Reading
    by heading needs no boundary at all, and names found under a section heading are by
    construction names the page states, so accuracy holds too.

    Shaped like real grounded prose rather than a name dump, because several tests assert on
    what a page's SECTIONS do: section-to-symbol linking, subsystem naming, partition node
    indices. Each name appears twice, once backticked and once bare, because the symbol
    linker matches plain names in prose -- a name living only inside backticks is linked by
    nothing, which is how a draft passes every gate and still yields a page with no links.
    """
    from contextlake.kb.wiki.structural import SECTION_TITLES

    out: list[str] = []
    for title in SECTION_TITLES.values():
        marker = f"## {title}"
        if marker not in prompt:
            continue
        section = prompt.split(marker, 1)[1].split("\n## ", 1)[0]
        names = backticked_names(section)
        if not names:
            continue
        out.append(marker)
        out.append("")
        for name in names:
            out.append(f"`{name}`: {name} is recorded under {title.lower()}.")
        out.append("")
    if not out:
        # No structural page in the prompt, which means the caller invoked `generate_page`
        # directly rather than through `cmd_wiki`. There is no replacement gate on that
        # path, so completeness is not being asked of this draft, and the historical
        # sentence is what those tests assert on. It carries SOUND_MARKER too so a test can
        # assert "a generated page reached disk" without caring which path produced it.
        return ("## Overview\n\nForecastService samples the grid, and that is recorded "
                "under overview.\n")
    return "\n".join(out).rstrip() + "\n"


# What a `sound_draft` page always contains, for tests that only need to assert "the
# generated page reached disk". Asserting on a fixed sentence stopped working when the
# draft became derived from the prompt, and asserting on a symbol name would couple every
# such test to one fixture's shard.
SOUND_MARKER = "is recorded under"
