# Page types and structure

Every contextlake doc page is exactly one type, with a fixed shape. Typing pages is the cure for walls of
text: a page that braids explanation, steps, and lookup gets split. This page is part of the
[documentation style guide](style-guide.md).

## The page types

Sort each section into a type. If a how-to grows three paragraphs of background, that background is a
concept page in disguise. Signal the type through the nav group and the title form, not an ugly filename
prefix. Keep URLs clean.

### Concept

Answers "what is this, and why care?".

- **Title:** a noun phrase. "The knowledge layer", not "Understanding the knowledge layer".
- **Skeleton:** a lead summary, then explanation in prose, lists, and a code-grounded diagram. Begin with a
  one-sentence definition. No step-by-step instructions; those belong in a how-to.

### How-to (task)

Answers "how do I do X?".

- **Title:** a gerund phrase. "Indexing your repos", "Wiring your editor".
- **Skeleton:**
  1. A lead that names what you'll accomplish and why.
  2. **Prerequisites** (what must be true first).
  3. Numbered, imperative steps. One action per step. State where before what ("In `kb.toml`, set...").
  4. **Verification.** How you know it worked, with the real output to expect. Near-required; it's the most
     reader-respecting thing a CLI doc can do.
  5. **Next steps** or **See also.**

### Reference

Answers "what are the exact details?".

- **Title:** a noun phrase. "`contextlake` command reference", "Configuration keys".
- **Skeleton:** a short lead, then scannable tables and lists (flags, keys, exit codes). Facts, not
  narrative. Keep subsections consistent across sibling reference pages.

### Tutorial

Answers "walk me through it end to end".

- **Title:** a gerund or "Getting started".
- **Skeleton:** a lead stating prerequisites and the end state, then a narrated, ordered path with
  checkpoints. Teaching-oriented, so a little more prose than a how-to is fine.

### Overview / landing

Answers "where am I, and where do I go next?".

- **Title:** a noun phrase.
- **Skeleton:** a short orienting lead, then signposts to the pages above.

## Rules that make typing work

- **One topic per page.** A page must make sense on its own.
- **No stubs.** A page covers its topic completely (every flag, the edge cases, at least one example) or it
  isn't ready. A thin page is worse than no page, because it looks finished.
- **The Verification step earns its keep.** "You should see `4 repos, 29 nodes, 28 edges`" turns a hopeful
  reader into a confident one, and it dogfoods the grounded ethos.

## Structure within a page

- **Lead first.** Open every page with a one-to-three-sentence, plain-language summary of what it is and
  why you'd use it, before any section heading. A reader who stops after the lead still leaves with the
  gist. This is the highest-leverage fix for scannability.
- **One H1, body starts at H2, never skip a level.** The site strips the H1 and shows the hero title, so
  your first real heading is H2.
- **Short paragraphs, three to seven lines.** Single-line paragraphs are fine. Break up anything longer.
- **Front-load the keyword** in headings, list items, and the first words of a paragraph. Readers scan in
  an F-shape.
- **A section longer than about forty lines gets sub-headings.** Never use a bold inline run as a stand-in
  for a heading.
- **End with "See also"** (or "Next steps" for a task): a short bulleted list of links, internal before
  external, no trailing punctuation.
- **Long pages rely on the site's "On this page" rail** for in-page navigation. Don't hand-maintain a
  table of contents.

## See also

- [Documentation style guide](style-guide.md)
- [Voice and tone](style-guide-voice.md)
- [Formatting](style-guide-formatting.md)
