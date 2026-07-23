# Formatting, accessibility, and inclusive language

The mechanics: how headings, lists, code, callouts, links, and images look and read, plus the
accessibility and inclusive-language rules that are correctness, not decoration. This page is part of the
[documentation style guide](style-guide.md).

## Headings

Sentence case. "Building the knowledge layer", not "Building The Knowledge Layer". No terminal period or
colon. Unique and descriptive within the page, because each becomes an anchor link. No vague openers, so
"Understanding indexing" becomes "Indexing". No questions except in an FAQ or troubleshooting section.
Parallel across peers (all gerunds, or all nouns). Never stack two headings with no text between them, and
never leave a lone single sub-section, have two or none.

## Lists

- Introduce a list with a complete sentence ending in a colon. Not a fragment like "Examples include:".
- Keep items parallel: all imperative, or all noun phrases, never mixed.
- Use all-or-none end punctuation. If every item is a full sentence, end each with a period. If all are
  fragments, use none.
- Bullet when order doesn't matter; number when it does, or when you'll refer to a step by its number.
- Aim for at most about seven items, and nest at most two levels deep. A single-step procedure is a
  sentence, not a one-item list.

## Tables

Use tables for genuinely tabular data: flags, config keys, provider matrices, comparisons. Don't use a
table to lay out prose. Give every table a short lead-in sentence, sentence-case column headers, and
specific rather than generic headers ("Config key", not "Item").

## Code and command examples

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
- **A filename is an adjective, not a noun.** "Edit the `kb.toml` file", not "edit `kb.toml`". Read an
  extension aloud as "dot" ("a `.tf` file").
- **Straight quotes only inside code and commands.** Smart quotes break copy-paste.
- **Enclose config tags in angle brackets** and don't split a sentence across a code block.

## Callouts

Use a small, named set, sparingly. One strong callout beats five buried in prose. A callout stands alone;
never bury it inline in a paragraph.

| Callout | Use for |
|---|---|
| **Note** | a neutral aside worth surfacing |
| **Tip** | a shortcut or a better path |
| **Important** | a prerequisite or constraint the reader must not miss |
| **Warning** | a destructive or irreversible action (a sync over a dirty tree, a forced branch switch) |

Pick the weakest level that fits, and don't cry wolf. Reserve **Warning** for the genuinely destructive,
matching the CLI's own safety-flag posture.

## Links

- Use descriptive link text that makes sense out of context. Never "click here", "this", "here", or a
  bare URL. Screen-reader users navigate by a list of links stripped of surrounding text.
- Name the section for a same-page reference ("see Backends and tuning"), never "below" or "above".
- Write cross-doc links in the form the site rewriter normalizes (a bare `foo.md` between sibling docs,
  `docs/foo.md` from the README) so they resolve on both GitHub and the site.
- Put related links in a "See also" block at the end.

## Numbers, dates, and units

- Use numerals for anything technical: versions, counts, sizes, flags, percentages ("5 MB", "v2.44.0",
  "5%"). Spell out zero through nine only in ordinary prose.
- Write bytes as `B` and bits as `b`; never let a prefix stand alone ("16 KB", not "16 K").
- Never write a date numerically. "15 May 2026", not "5/15/26", which is ambiguous across locales.

## Capitalization and punctuation

- Sentence case for headings, UI text, and generic terms. Capitalize only proper nouns and trademarks.
- **"contextlake" is always one lowercase word,** even at the start of a sentence. Reword rather than
  capitalize it.
- Don't invent internal caps ("copy pool", not "CopyPool"). Match the interface's capitalization when you
  name a UI element.
- Use the serial (Oxford) comma.
- **No em-dashes anywhere.** Use a comma, a colon, parentheses, or a period.

## Accessibility

These improve the rendered site for everyone.

- **Alt text on every image,** describing its function, not its appearance. Empty alt text for a purely
  decorative image; a longer description for a data-carrying visual.
- **Never put information only in an image.** Pair every diagram with prose that carries the same fact.
- **Never rely on color alone.** contextlake's CLI already dual-encodes status with glyphs
  (`✓ ⚠ ✗ ⊘ = ↝ ~`) plus color; that's the standard. Extend it to diagrams: distinguish node kinds by
  shape and label, not hue alone.
- **Descriptive, standalone link text** (also an accessibility rule).
- **Don't use direction as the only locator.** Name the thing ("the Fleet overview tab"), not "the tab on
  the right".
- **Convey hierarchy with heading levels, not bold text,** and never force a line break inside a sentence.
- **Give tables real headers and a short description** so a screen reader can convey the cell relationships.

## Inclusive language

Use neutral, precise terms. These substitutions are field-standard across Google, MDN, and Red Hat.

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

## Diagrams and visuals

- **Code-grounded, always.** Every diagram traces to real structure. The knowledge-graph taxonomy diagram
  imports its colors from `kb/visualize.py` so it matches `contextlake graph` output exactly.
- **Generated, not hand-drawn.** Flat diagrams come from `site/tools/gen_diagrams.py` (SVG, brand palette,
  transparent), rasterized with cairosvg where a PNG is needed. Don't use an image model for a flat
  diagram; it garbles labels. Painterly Pebble scene art is a separate, decorative register.
- **Lead with real CLI screenshots** where they teach best, a code-grounded diagram where a picture
  replaces more than a hundred words of prose, and painterly brand art last and sparingly.
- Give every visual alt text, and a caption on the site where it aids scanning.

## See also

- [Documentation style guide](style-guide.md)
- [Page types and structure](style-guide-structure.md)
- [Word and term reference](style-guide-reference.md)
