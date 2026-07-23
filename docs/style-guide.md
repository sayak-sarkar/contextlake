# Documentation style guide

How we write contextlake: its docs, and by extension its CLI text, dashboard copy, and website. This is
the writing companion to the [brand guidelines](BRANDING.md). The brand guidelines decide who contextlake
is; this guide decides how it talks. Where the two overlap, the brand guidelines win on voice and this
guide wins on structure.

If you write or edit a single page, you need only three things: the spirit (below), the page type you're
writing (section 4), and the review checklist at the end. Everything between is the reference you reach
for when a specific question comes up.

## The spirit: grounded, lucid, warm

contextlake's docs sound like a knowledgeable friend who read your code so the model doesn't have to
guess. Three brand words govern every page.

**Grounded.** Every claim traces to something real: a command, a file, a flag, a line of output. We show
the receipt. We never overclaim. When something is a deliberate limitation, we say so plainly, because an
honest boundary builds more trust than a hidden one.

**Lucid.** Short sentences. Plain words. One idea per line. We write for scanning first and reading
second. If a sentence serves none of the three brand words, we cut it.

**Warm.** We talk to you, not at you. Contractions are welcome. The tone is friendly, never cutesy, never
corporate. Pebble is on your side.

The one-line test for any page: *structure it like a reference, decide once like a ledger, and talk like
a friend.*

---

## 1. Voice and tone

This extends section 1.6 of the brand guidelines into doc-length writing.

### The defaults

These five are mechanical. A linter can check them, and reviewers should.

- **Second person.** Address the reader as "you". The reader owns the lake. Reserve "we" for a genuine
  maintainer recommendation ("we recommend Ollama at scale"), never as a synonym for "you".
- **Present tense.** "`sync` fetches and updates every repo", not "will fetch".
- **Active voice.** "Run `contextlake index`", not "the index should be run". Passive is acceptable only
  when the actor is genuinely unknown or irrelevant, or in an error message where you don't want to blame
  the reader.
- **Imperative for steps.** Every step in a task starts with a verb: "Install", "Run", "Open", "Verify".
- **Contractions welcome.** "you'll", "it's", "let's", "don't". This is the heart of the warmth. Avoid a
  contraction only where it creates real ambiguity in a precision-critical spot.

### Warmth, calibrated

Friendly, not frivolous. Use this three-way test when a sentence feels off:

| Too informal | Just right | Too formal |
|---|---|---|
| "Dude, this indexes EVERYTHING, so sick." | "`index` walks every repo under a folder and builds the graph." | "The index subsystem facilitates the construction of a graph representation of the corpus." |

Warmth lives in direct address, contractions, and the occasional light human touch. It does not live in
slang, hype, or jokes that don't translate. When in doubt, read the sentence aloud. If it doesn't sound
like something you'd say to a colleague you respect, rewrite it.

### Grounded writing: the anti-hallucination habits

These are contextlake's signature. They are also just good technical writing.

- **Claim, then citation.** Wherever a page states what the tool does, show where it comes from: "the
  count comes from `graph_stats`", "you'll see `1976 vectors written`". A cited noun out-persuades any
  adjective. This is the single most on-brand thing our docs can do.
- **Kill the filler that gaslights a stuck reader.** Delete "just", "simply", "easy", and "easily". If
  someone is reading the docs, the thing wasn't obvious, and calling it easy only makes a stuck reader
  feel worse.
- **Measured language.** "usually", "in most cases", and "often" beat false absolutes. The promise is
  provenance, that an answer traces to real source, not infallibility.
- **Examples support text, they never replace it.** Never expect the reader to extract the instruction
  from a bare code block. Say what the block does, then show it, then say what came back.

### Banned words and tics

- **Hype:** leverage, seamless, powerful, revolutionary, supercharge, next-gen, robust, cutting-edge,
  unleash, and "intelligence" as a noun. Rewrite with a concrete verb or noun.
- **Overclaims:** "100% accurate", "never wrong", "eliminates hallucinations", "always correct",
  "guarantees". These break *grounded*.
- **Anthropomorphism:** no "allows you to", "lets you", or "enables you to". Use a reader-focused verb.
  Write "Use the dashboard to inspect a repo", not "the dashboard lets you inspect a repo". (Software may
  still "detect", "display", "read", or "prompt". Those are fine.)
- **"please"** in instructions. It's superfluous and reads unevenly across cultures. Write "To view the
  graph, run `contextlake graph`", not "Please run".
- **"click here" / "read this"** as link text. See section 6.6.
- **Exclamation points** in body copy. One is allowed, rarely, in the warmest microcopy.

---

## 2. Word choice and terminology

Clear words, used consistently, are most of what makes docs feel professional.

### Prefer the simple word

Use the shorter, plainer option: "use" not "utilize", "with" or "through" not "via", "for example" not
"e.g.", "that is" not "i.e.", "about" not "approximately". Spell out Latin abbreviations in running prose;
reserve "e.g." and "i.e." for parentheses if you must.

### Use the same word for the same thing

If you mean the graph, say "the graph" every time. Don't reach for a synonym to avoid repetition;
synonym-variety reads as elegance to the writer and as ambiguity to the reader. This matters most for
contextlake's own concepts, which have precise meanings (see the ledger in section 10).

### Precise verbs

- **enter** text (any input method), **type** only when you mean the keyboard specifically.
- **select** a checkbox or an option, don't "check" it.
- **run** a command, **pass** a flag, **set** a config key.
- **stop** or **cancel** a process, never "kill" or "abort" (see section 8).

### Abbreviations

Define every abbreviation on first use on each page: MCP (Model Context Protocol), AST (abstract syntax
tree), RAG (retrieval-augmented generation), ANN (approximate nearest neighbor), FTS (full-text search),
SME (subject-matter expert). After the first mention, the short form is fine.

---

## 3. The reader, and writing for everyone

Assume your reader is smart, busy, and possibly reading English as a second language. That single
assumption drives most of the rules below.

- **Short sentences.** Aim under 25 words. Split a long sentence into two, or into a list.
- **Keep "that".** "Verify that the service is running" is easier to parse than "Verify the service is
  running", especially in translation.
- **Avoid idioms and figurative load-bearing language.** "Solve two problems at once", not "kill two
  birds with one stone". A metaphor may decorate, but it must never carry the only copy of a technical
  fact. (The lake metaphor is the one sanctioned exception, and it is decorative, see section 10.)
- **Don't open with an expletive subject.** "The store holds three tables", not "There are three tables
  in the store". Expletive openers ("there is", "it is important to") hide the real subject.
- **One idea per sentence, one topic per paragraph,** and state the paragraph's point in its first
  sentence.

---

## 4. Page types

Every page is exactly one type. This is the shared lesson of the field's best guides, and it is the cure
for walls of text: a page that braids explanation, steps, and lookup gets split. Sort each section into a
type. If a how-to grows three paragraphs of background, that background is a concept page in disguise.

Signal the type through the folder or nav group and the title form, not through an ugly filename prefix.
Keep URLs clean.

### Concept

Answers "what is this, and why care?".

- **Title:** a noun phrase. "The knowledge layer", not "Understanding the knowledge layer".
- **Skeleton:** a lead summary, then explanation in prose, lists, and a code-grounded diagram. Begin with
  a one-sentence definition. No step-by-step instructions; those belong in a how-to.

### How-to (task)

Answers "how do I do X?".

- **Title:** a gerund phrase. "Indexing your repos", "Wiring your editor".
- **Skeleton:**
  1. A lead that names what you'll accomplish and why.
  2. **Prerequisites** (what must be true first).
  3. Numbered, imperative steps. One action per step. State where before what ("In `kb.toml`, set...").
  4. **Verification.** How you know it worked, with the real output to expect. This step is near-required;
     it is the most reader-respecting thing a CLI doc can do.
  5. **Next steps** or **See also.**

### Reference

Answers "what are the exact details?".

- **Title:** a noun phrase. "`contextlake` command reference", "Configuration keys".
- **Skeleton:** a short lead, then scannable tables and lists (flags, keys, exit codes). Facts, not
  narrative. Keep subsections consistent across sibling reference pages (for a command: summary, synopsis,
  options, examples, related commands).

### Tutorial

Answers "walk me through it end to end".

- **Title:** a gerund or "Getting started".
- **Skeleton:** a lead stating prerequisites and the end state, then a narrated, ordered path with
  checkpoints along the way. Teaching-oriented, so a little more prose than a how-to is fine.

### Overview / landing

Answers "where am I, and where do I go next?".

- **Title:** a noun phrase.
- **Skeleton:** a short orienting lead, then signposts to the pages above.

**Rules that make typing work:**

- **One topic per page.** A page must make sense on its own.
- **No stubs.** A page covers its topic completely (every flag, the edge cases, at least one example) or
  it isn't ready. A thin page is worse than no page, because it looks finished.
- **The Verification step earns its keep.** "You should see `4 repos, 29 nodes, 28 edges`" turns a
  hopeful reader into a confident one, and it dogfoods the grounded ethos.

---

## 5. Structure within a page

- **Lead first.** Open every page with a one-to-three-sentence, plain-language summary of what it is and
  why you'd use it, before any section heading. A reader who stops after the lead still leaves with the
  gist. This is the highest-leverage fix for scannability.
- **One H1, body starts at H2, never skip a level.** The site strips the H1 and shows the hero title, so
  your first real heading is H2.
- **Short paragraphs, three to seven lines.** Single-line paragraphs are fine. Break up anything longer.
- **Front-load the keyword** in headings, list items, and the first words of a paragraph. Readers scan in
  an F-shape, so the scannable word goes first.
- **A section longer than about forty lines gets sub-headings.** Never use a bold inline run as a
  stand-in for a heading.
- **End with "See also"** (or "Next steps" for a task): a short bulleted list of links, internal before
  external, with no trailing punctuation.
- **Long pages rely on the site's "On this page" rail** for in-page navigation. Don't hand-maintain a
  table of contents.

---

## 6. Formatting

### 6.1 Headings

Sentence case. "Building the knowledge layer", not "Building The Knowledge Layer". No terminal period or
colon. Unique and descriptive within the page, because each one becomes an anchor link. No vague openers,
so "Understanding indexing" becomes "Indexing". No questions except in an FAQ or troubleshooting section.
Parallel across peers (all gerunds, or all nouns). Never stack two headings with no text between them, and
never leave a lone single sub-section, have two or none.

### 6.2 Lists

- Introduce a list with a complete sentence ending in a colon. Not a fragment like "Examples include:".
- Keep items parallel: all imperative, or all noun phrases, never mixed.
- Use all-or-none end punctuation. If every item is a full sentence, end each with a period. If all are
  fragments, use none. Don't mix.
- Bullet when order doesn't matter; number when it does, or when you'll refer to a step by its number.
- Aim for at most about seven items, and nest at most two levels deep. A single-step procedure is a
  sentence, not a one-item list.

### 6.3 Tables

Use tables for genuinely tabular data: flags, config keys, provider matrices, comparisons. Don't use a
table to lay out prose. Give every table a short lead-in sentence, sentence-case column headers, and
specific rather than generic headers ("Config key", not "Item").

### 6.4 Code and command examples

Every non-trivial example follows the four-part unit:

1. A short lead-in sentence ending in a colon, or a scenario sub-heading.
2. One sentence on what the command does.
3. The code block: language-tagged (the site requires an explicit language), one command per block, the
   command separated from its output.
4. What the output means, especially the line the reader should see to know it worked.

More conventions:

- **Monospace** (backticks) for commands, flags, filenames, paths, config keys, values the reader types,
  and literal output.
- **Replaceable values** as `<placeholder>`: angle brackets, lowercase, descriptive. Mark trimmed output
  with a comment like `# ...output omitted...`.
- **A filename is an adjective, not a noun.** "Edit the `kb.toml` file", not "edit `kb.toml`". Include the
  leading dot in an extension and read it aloud as "dot" ("a `.tf` file").
- **Straight quotes only inside code and commands.** Smart quotes break copy-paste.
- **Enclose config tags in angle brackets** and don't split a sentence across a code block.

### 6.5 Callouts

Use a small, named set, and use it sparingly. One strong callout beats five buried in prose. A callout
stands alone; never bury it inline in a paragraph.

| Callout | Use for |
|---|---|
| **Note** | a neutral aside worth surfacing |
| **Tip** | a shortcut or a better path |
| **Important** | a prerequisite or constraint the reader must not miss |
| **Warning** | a destructive or irreversible action (a sync over a dirty tree, a forced branch switch) |

Pick the weakest level that fits, and don't cry wolf. Reserve **Warning** for the genuinely destructive,
matching the CLI's own safety-flag posture.

### 6.6 Links

- Use descriptive link text that makes sense out of context. Never "click here", "this", "here", or a
  bare URL. Screen-reader users navigate by a list of links stripped of surrounding text.
- Name the section for a same-page reference ("see [Backends and tuning](#backends-and-tuning)"), never
  "below" or "above".
- Write cross-doc links in the form the site rewriter normalizes (a bare `foo.md` between sibling docs,
  `docs/foo.md` from the README) so they resolve on both GitHub and the site.
- Put related links in a "See also" block at the end. Use an inline link only where the reader genuinely
  must leave the page right now.

### 6.7 Numbers, dates, and units

- Use numerals for anything technical: versions, counts, sizes, flags, percentages ("5 MB", "v2.44.0",
  "5%"). Spell out zero through nine only in ordinary prose.
- Write bytes as `B` and bits as `b`; never let a prefix stand alone ("16 KB", not "16 K").
- Never write a date numerically. "15 May 2026", not "5/15/26", which is ambiguous across locales.

### 6.8 Capitalization and punctuation

- Sentence case for headings, UI text, and generic terms. Capitalize only proper nouns and trademarks.
- **"contextlake" is always one lowercase word,** even at the start of a sentence. Reword rather than
  capitalize it. Never "Context Lake", "ContextLake", or split across a line.
- Don't invent internal caps ("copy pool", not "CopyPool"). Match the interface's capitalization when you
  name a UI element.
- Use the serial (Oxford) comma.
- **No em-dashes anywhere.** Use a comma, a colon, parentheses, or a period. Every reference guide forbids
  em-dashes in technical writing, and it's contextlake house style besides.

---

## 7. Accessibility

These are correctness, not decoration, and they improve the rendered site for everyone.

- **Alt text on every image,** describing its function, not its appearance. Use empty alt text for a
  purely decorative image, and add a longer description for a data-carrying visual.
- **Never put information only in an image.** Pair every diagram with prose that carries the same fact.
- **Never rely on color alone.** contextlake's CLI already dual-encodes status with glyphs
  (`✓ ⚠ ✗ ⊘ = ↝ ~`) plus color; that is the standard. Extend it to the graph and C4 diagrams: distinguish
  node kinds by shape and label, not by hue alone.
- **Use descriptive, standalone link text** (also an accessibility rule, see section 6.6).
- **Don't use direction as the only locator.** Name the thing ("the Fleet overview tab"), not "the tab on
  the right".
- **Convey hierarchy with heading levels, not bold text,** and never force a line break inside a sentence.
- **Give tables real headers and a short description** so a screen reader can convey the cell
  relationships.

---

## 8. Inclusive language

Use neutral, precise terms. The substitutions below are field-standard across Google, MDN, and Red Hat.

| Avoid | Use |
|---|---|
| whitelist / blacklist | allowlist / blocklist (or denylist) |
| master / slave | primary / replica, controller / device, source / replica |
| kill / abort (a process) | stop, cancel, end, exit |
| dummy value | placeholder |
| sanity check | confidence check, final check |
| crazy, insane, dumb, blind to | confusing, baffling, unaware of |
| man-hours, mankind | person-hours, humanity |
| native (speaker or feature) | omit, or name the specific language |

Use singular "they" for a person of unknown gender, or make the subject plural, or drop the pronoun.
Never default to "he" or "she". Use diverse, realistic names in examples.

---

## 9. Diagrams and visuals

- **Code-grounded, always.** Every diagram traces to real structure. The knowledge-graph taxonomy diagram
  imports its colors from `kb/visualize.py` so it matches `contextlake graph` output exactly.
  Anti-hallucination applies to pictures too.
- **Generated, not hand-drawn.** Flat diagrams come from `site/tools/gen_diagrams.py` (SVG, brand palette,
  transparent background), rasterized with cairosvg where a PNG is needed. Don't use an image model for a
  flat diagram; it garbles labels and structure. Painterly Pebble scene art is a separate register, it is
  decorative, and it is never load-bearing.
- **Lead with real CLI screenshots** where they teach best, use a code-grounded diagram where a picture
  replaces more than a hundred words of prose, and use painterly brand art last and sparingly.
- Give every visual alt text, and a caption on the site where it aids scanning.

---

## 10. The house-style decision cache

A style guide's real value is removing per-instance deliberation. This is the lookup table for
contextlake's recurring micro-choices, so that no contributor, and no agent, re-decides them. When you
make a new recurring decision, add it here.

- **The name:** `contextlake`, always one lowercase word. Never "Context Lake", "ContextLake", or
  capitalized mid-sentence.
- **The category noun:** "context layer". Never "tool", "platform", "framework", "knowledge base", or
  "data lake".
- **One term per concept.** These mean specific things; don't use them as loose synonyms:
  - **the graph** is the extracted node-and-edge structure.
  - **the knowledge layer** is the whole optional subsystem (`contextlake.kb`).
  - **the index** is the built store; **indexing** is the act of building it.
  - **a repo** in running prose (match the CLI), not "repository".
  - command names (`sync`, `index`, `graph`, `wiki`, `serve`) are lowercase and in `code`.
- **The language count:** "14 languages across 13 tree-sitter grammars" (`.tsx` shares the TypeScript
  grammar). Use this exact phrasing everywhere.
- **The lake metaphor** is a closed system (brand guidelines section 1.3): *deep* is the real, complete
  source underneath, and *clear* is the precise answer back. Never oceans, waves, fishing, drowning, or
  "data lake". Depth is calm and legible, never threatening.
- **Example values only:** `example.com` and `.org`, `127.0.0.1`, and the public `pallets` GitHub org we
  dogfood on. Never a real private host, token, or internal path.
- **Abbreviations:** define on first use per page (MCP, AST, RAG, ANN, FTS, SME).

---

## 11. Beyond the docs: CLI, dashboard, and website

This guide is the writing layer of the brand, so it shapes every surface where contextlake uses words.

- **CLI text.** Help strings, status lines, and errors follow the same voice: grounded (show the real
  path or count), lucid (one idea per line), warm (plain and direct). Status is never conveyed by color
  alone. Remember that the best doc is the one you don't need: invest in self-explanatory output and
  empty-state guidance first, then let the docs cover what the CLI can't inline.
- **Dashboard copy.** Labels, empty states, and tooltips use sentence case, second person, and the same
  callout discipline as the docs.
- **Website.** Hero and marketing copy follow the brand guidelines' tagline hierarchy; body and docs
  follow this guide. Sentence case, no em-dashes, and a cited noun over an adjective, everywhere.

---

## 12. Enforcement

Consistency survives many editing sessions (human and agent) only if it's checked, not just hoped for.

- **A page review checklist** (below) runs against every page before it ships.
- **A lint config** (Vale or equivalent) encodes the mechanical rules: the banned hype words, "click
  here", the filler words, em-dashes, and "allows you to". Style becomes a build gate like `ruff`, not a
  review argument. Keep the ruleset small and focused.
- **The site's `de_emdash` step** stays as a backstop, but source should already be em-dash-free.

### Page review checklist

1. The page is one type, titled correctly (gerund task, noun concept, noun reference).
2. A lead summary sits in the first one to three sentences.
3. Headings are sentence case, unique, and parallel; no bold-run fake headings.
4. Voice is second person, present tense, active, with contractions; no hype, no "allows you to", no
   filler words.
5. Every claim shows its receipt; nothing overclaims.
6. Code examples follow the four-part unit; commands are monospace; placeholders are in `<angle brackets>`.
7. Every how-to has a Verification step.
8. Links are descriptive (no "click here", no "below"); a "See also" block closes the page.
9. Every image has alt text; status and diagrams aren't color-only; terms are inclusive.
10. Zero em-dashes; "contextlake" is lowercase; abbreviations are defined; only example values appear.

---

## Lineage

This guide synthesizes seven widely respected references and reshapes them around contextlake's brand:
the [Red Hat modular documentation](https://redhat-documentation.github.io/modular-docs/) and
[supplementary style guide](https://redhat-documentation.github.io/supplementary-style-guide/), the IBM
Style Guide, the [Google developer documentation style guide](https://developers.google.com/style), the
[Microsoft Writing Style Guide](https://learn.microsoft.com/en-us/style-guide/welcome/), the
[MDN writing guidelines](https://developer.mozilla.org/en-US/docs/MDN/Writing_guidelines), the
[Wikipedia Manual of Style](https://en.wikipedia.org/wiki/Wikipedia:Manual_of_Style), and the National
Geographic Style Manual. The structure comes mostly from Red Hat, IBM, MDN, and Wikipedia; the warm voice
comes mostly from Google and Microsoft; the discipline of a written-down decision cache comes from
National Geographic. Where those guides turn cold (no contractions, no second person), we keep the
structure and the warmth, and leave the coldness behind.
