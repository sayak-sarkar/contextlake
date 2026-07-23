# Voice and tone

How contextlake sounds: warm, grounded, and clear. This page covers the voice defaults, word choice, and
writing for every reader. It extends section 1.6 of the [brand guidelines](brand.md) into doc-length
writing, and it's part of the [documentation style guide](style-guide.md).

## The defaults

These five are mechanical. A linter can check them, and reviewers should.

- **Second person.** Address the reader as "you". The reader owns the lake. Reserve "we" for a genuine
  maintainer recommendation ("we recommend Ollama at scale"), never as a synonym for "you".
- **Present tense.** "`sync` fetches and updates every repo", not "will fetch".
- **Active voice.** "Run `contextlake index`", not "the index should be run". Passive is fine only when the
  actor is genuinely unknown or irrelevant, or in an error message where you don't want to blame the reader.
- **Imperative for steps.** Every step in a task starts with a verb: "Install", "Run", "Open", "Verify".
- **Contractions welcome.** "you'll", "it's", "let's", "don't". This is the heart of the warmth. Avoid a
  contraction only where it creates real ambiguity in a precision-critical spot.

## Warmth, calibrated

Friendly, not frivolous. Use this three-way test when a sentence feels off:

| Too informal | Just right | Too formal |
|---|---|---|
| "Dude, this indexes EVERYTHING, so sick." | "`index` walks every repo under a folder and builds the graph." | "The index subsystem facilitates the construction of a graph representation of the corpus." |

Warmth lives in direct address, contractions, and the occasional light human touch. It does not live in
slang, hype, or jokes that don't translate. When in doubt, read the sentence aloud. If it doesn't sound
like something you'd say to a colleague you respect, rewrite it.

## Grounded writing: the anti-hallucination habits

These are contextlake's signature, and they're also just good technical writing.

- **Claim, then citation.** Wherever a page states what the tool does, show where it comes from: "the count
  comes from `graph_stats`", "you'll see `1976 vectors written`". A cited noun out-persuades any adjective.
  This is the most on-brand thing our docs can do.
- **Kill the filler that gaslights a stuck reader.** Delete "just", "simply", "easy", and "easily". If
  someone is reading the docs, the thing wasn't obvious.
- **Measured language.** "usually", "in most cases", and "often" beat false absolutes. The promise is
  provenance, that an answer traces to real source, not infallibility.
- **Examples support text, they never replace it.** Never expect the reader to extract the instruction from
  a bare code block. Say what it does, show it, then say what came back.

## Banned words and tics

- **Hype:** leverage, seamless, powerful, revolutionary, supercharge, next-gen, robust, cutting-edge,
  unleash, and "intelligence" as a noun. Rewrite with a concrete verb or noun.
- **Overclaims:** "100% accurate", "never wrong", "eliminates hallucinations", "guarantees". These break
  *grounded*.
- **Anthropomorphism:** no "allows you to", "lets you", or "enables you to". Use a reader-focused verb:
  "Use the dashboard to inspect a repo", not "the dashboard lets you inspect a repo". (Software may still
  "detect", "display", "read", or "prompt".)
- **"please"** in instructions. Write "To view the graph, run `contextlake graph`", not "Please run".
- **"click here" / "read this"** as link text. See the [formatting page](style-guide-formatting.md).
- **Exclamation points** in body copy. One is allowed, rarely, in the warmest microcopy.

## Word choice

- **Prefer the simple word:** "use" not "utilize", "with" or "through" not "via", "for example" not "e.g."
  (in prose), "that is" not "i.e.", "about" not "approximately".
- **Use the same word for the same thing.** If you mean the graph, say "the graph" every time. Synonym
  variety reads as ambiguity to the reader. This matters most for contextlake's own concepts, which have
  precise meanings (see the [word reference](style-guide-reference.md)).
- **Precise verbs:** *enter* text (any method) vs *type* (keyboard specifically); *select* an option, don't
  "check" it; *run* a command, *pass* a flag, *set* a config key; *stop* or *cancel* a process, never "kill".

## Write for every reader

Assume your reader is smart, busy, and possibly reading English as a second language.

- **Short sentences,** aim under 25 words. Split a long one into two, or into a list.
- **Keep "that".** "Verify that the service is running" parses more easily than "Verify the service is
  running", especially in translation.
- **Avoid idioms and figurative load-bearing language.** A metaphor may decorate, but it must never carry
  the only copy of a technical fact. (The lake metaphor is the one sanctioned decorative exception.)
- **Don't open with an expletive subject.** "The store holds three tables", not "There are three tables in
  the store".
- **Define every abbreviation on first use** per page (MCP, AST, RAG, ANN, FTS, SME).

## See also

- [Documentation style guide](style-guide.md)
- [Page types and structure](style-guide-structure.md)
- [Word and term reference](style-guide-reference.md)
