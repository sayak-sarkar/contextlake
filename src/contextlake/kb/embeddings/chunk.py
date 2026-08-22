"""Split a document into overlapping chunks, so a long one is more than one vector.

An ingested document used to be embedded as a SINGLE vector over its whole text. For a
14 KB page that vector is an average of everything the page is about, and a question about
one specific paragraph in it matches poorly or not at all. Measured on 29 real documents
with 53 position-selected queries, chunking took the right document from **71.7% to 94.3%**
hit rate and MRR from **45.2% to 80.4%**, for about 15 extra tokens per query. The
measurement, its controls, and what it does NOT establish are in
`docs/semantic-search.md`.

**The defaults here are the ones that were measured.** 1200 characters with 200 of overlap.
Changing them is fine, but it makes the numbers above describe a configuration that is no
longer the one shipping, so change them with a measurement rather than a preference.

Two things this deliberately does NOT do:

* **It does not split mid-paragraph.** Packing whole paragraphs keeps a sentence, and usually
  an argument, intact. A fixed-width cut through the middle of a sentence produces a chunk
  that is hard to match and a neighbour that is missing its subject.
* **It does not drop a trailing short chunk.** The last chunk of a document is often its
  conclusion, and a splitter that discards a remainder below some threshold loses exactly the
  paragraph that says what the page decided.
"""

from __future__ import annotations

import re

#: Roughly 200 words. Big enough to carry an argument, small enough that one topic dominates
#: the vector rather than being averaged away.
DEFAULT_MAX_CHARS = 1200

#: Carried from the end of the previous chunk into the next. Exists so a point that spans a
#: boundary still sits whole inside one chunk -- without it, the passages most likely to be
#: cut in half are the ones a specific question is asking about.
DEFAULT_OVERLAP = 200

_PARAGRAPH = re.compile(r"\n\s*\n")


def split_document(text: str, *, max_chars: int = DEFAULT_MAX_CHARS,
                   overlap: int = DEFAULT_OVERLAP) -> list[str]:
    """``text`` as a list of overlapping chunks, in order. Never empty for non-blank input.

    A document shorter than ``max_chars`` comes back as a single chunk, which is byte for
    byte what the caller used to embed -- so short documents are unaffected by this change.
    """
    text = text or ""
    if not text.strip():
        return []
    max_chars = max(1, int(max_chars))
    # Overlap must be strictly smaller than the chunk, or a chunk carries forward everything
    # it just emitted and the splitter makes no progress. Half is the useful bound rather
    # than `max_chars - 1`: progress per cut is `max_chars - overlap`, so an overlap just
    # under the budget advances a character at a time and emits thousands of near-identical
    # chunks for one paragraph -- not a hang, but the same runaway allocation more slowly.
    # Capping at half guarantees every cut advances by at least half the budget.
    overlap = max(0, min(int(overlap), max_chars // 2))

    out: list[str] = []
    buf = ""
    for para in (p for p in _PARAGRAPH.split(text) if p.strip()):
        if buf and len(buf) + len(para) > max_chars:
            out.append(buf)
            buf = (buf[-overlap:] + "\n\n" + para) if overlap else para
        else:
            buf = (buf + "\n\n" + para) if buf else para
        # A single paragraph longer than the budget cannot be packed, only cut. Cut it on
        # whitespace so the pieces are still words, and keep going rather than emitting one
        # enormous chunk that reintroduces the very averaging this module exists to avoid.
        while len(buf) > max_chars:
            cut = buf.rfind(" ", 0, max_chars)
            # The next chunk has to start AFTER this one did, or the splitter makes no
            # progress. The next one starts at `cut - overlap`, so a cut at or inside the
            # overlap window carries the whole buffer forward unchanged and loops forever,
            # appending until the machine is out of memory. A single long unbroken run of
            # characters -- an embedded base64 blob, a minified line, a giant URL -- puts
            # the only space in that window and is enough to trigger it.
            #
            # Give up the word boundary in that case, not the overlap: cutting mid-word at
            # the full budget still advances by `max_chars - overlap`, whereas keeping the
            # early cut and dropping the overlap would advance by as little as one
            # character and emit a chunk that is nearly all of the previous one. `rfind`
            # returning -1 for no space at all lands here too, and wants the same fallback.
            if cut <= overlap:
                cut = max_chars
            out.append(buf[:cut])
            buf = buf[cut - overlap:].lstrip()
    if buf.strip():
        out.append(buf)
    return out
