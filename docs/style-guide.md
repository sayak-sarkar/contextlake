# Documentation style guide

How we write contextlake: its docs, and by extension its CLI text, dashboard copy, and website. This is
the writing companion to the [brand guidelines](brand.md). The brand guidelines decide who contextlake is;
this guide decides how it talks.

This overview covers the spirit and the review checklist. The details live on four focused pages, linked
below, so you can jump straight to what you need instead of reading one long document.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/branding/pebble-peek-web.png" alt="Pebble, the contextlake otter, peeking over an edge and holding the glowing context-pebble." width="200">
</p>

## The spirit: grounded, lucid, warm

contextlake's docs sound like a knowledgeable friend who read your code so the model doesn't have to
guess. Pebble, the otter mascot, is that friend. Three brand words govern every page.

- **Grounded.** Every claim traces to something real: a command, a file, a flag, a line of output. We show
  the receipt. We never overclaim, and we state limitations plainly.
- **Lucid.** Short sentences. Plain words. One idea per line. We write for scanning first, reading second.
- **Warm.** We talk to you, not at you. Contractions are welcome. Friendly, never cutesy, never corporate.

The one-line test for any page: *structure it like a reference, decide once like a ledger, and talk like a
friend.*

## The four pages

- **[Voice and tone](style-guide-voice.md)**: the voice defaults, warmth, the grounded anti-hallucination
  habits, banned words, word choice, and writing for every reader.
- **[Page types and structure](style-guide-structure.md)**: the concept / how-to / reference / tutorial
  page types with their skeletons (including the Verification step), and how to structure a page.
- **[Formatting, accessibility, and inclusive language](style-guide-formatting.md)**: headings, lists,
  tables, the four-part code-example unit, callouts, links, numbers, capitalization, accessibility, and
  inclusive terms.
- **[Word and term reference](style-guide-reference.md)**: the house-style decision cache, worked
  before/after examples, and the A-to-Z term reference.

## Beyond the docs

This guide is the writing layer of the brand, so it shapes every surface where contextlake uses words.

- **CLI text** follows the same voice: grounded (show the real path or count), lucid (one idea per line),
  warm (plain and direct). Status is never conveyed by color alone. The best doc is the one you don't need,
  so invest in self-explanatory output and empty-state guidance first.
- **Dashboard copy** uses sentence case, second person, and the same callout discipline.
- **Website** hero copy follows the [brand guidelines](brand.md) tagline hierarchy; body and docs follow
  this guide.

## Enforcement

Consistency survives many editing sessions only if it's checked. A small lint config (Vale or equivalent)
encodes the mechanical rules (banned hype words, "click here", filler words, em-dashes, "allows you to"),
so style is a build gate like `ruff`, not a review argument. The site's `de_emdash` step stays as a
backstop, but source should already be em-dash-free.

## The page review checklist

Run this against every page before it ships.

1. The page is one type, titled correctly (gerund task, noun concept, noun reference).
2. A lead summary sits in the first one to three sentences.
3. Headings are sentence case, unique, and parallel; no bold-run fake headings.
4. Voice is second person, present tense, active, with contractions; no hype, no "allows you to", no filler.
5. Every claim shows its receipt; nothing overclaims.
6. Code examples follow the four-part unit; commands are monospace; placeholders are in `<angle brackets>`.
7. Every how-to has a Verification step.
8. Links are descriptive (no "click here", no "below"); a "See also" block closes the page.
9. Every image has alt text; status and diagrams aren't color-only; terms are inclusive.
10. Zero em-dashes; "contextlake" is lowercase; abbreviations are defined; only example values appear.

## References

For general technical-writing guidance beyond contextlake's own conventions, these public style guides are
worth reading:

- [Google developer documentation style guide](https://developers.google.com/style)
- [Microsoft Writing Style Guide](https://learn.microsoft.com/en-us/style-guide/welcome/)
- [MDN writing guidelines](https://developer.mozilla.org/en-US/docs/MDN/Writing_guidelines)
