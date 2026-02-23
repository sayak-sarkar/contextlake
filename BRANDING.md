# contextlake — brand guide

> **All your real context, in one local lake.**

**contextlake** is a local, always-fresh pool of everything you build with —
repositories, issues, designs, and (soon) images and models — mirrored, indexed, and
served to your AI tools so they answer from *real context* instead of guessing.

This is the single source of truth for the brand: name, voice, color, type, logo, and
mascot. Assets live in [`docs/branding/`](docs/branding/).

---

## The name

**contextlake** = **context** (what an AI needs to stop guessing) + **lake** (one local
pool, fed by many streams, deep enough to hold everything). Together: *a local lake of
real context your AI can draw from.*

It says what the tool does. "Context" is exactly the word people already use for what you
feed an AI; "lake" (as in *data lake*) says it's one pooled, local, queryable store rather
than scattered files. The name reads instantly to any developer.

**Why it scales.** A lake is fed by many streams. Today those streams are GitLab repos;
tomorrow they can be GitHub, Bitbucket, DockerHub, or Hugging Face — code, images, and
models are all just more **context** flowing into the same **lake**. Nothing about the name
binds it to one source, so the brand survives as scope grows.

- Always lowercase: **contextlake** (one word). Never "Context Lake", "ContextLake", or
  "context-lake".
- The package, command, and PyPI project are `contextlake`.

**It is:** a context layer · a local pool of real source · model-agnostic · always-fresh.
**It is not:** a chatbot · a cloud service · a code generator · tied to any one platform.

## Voice & tone

Grounded, plain-spoken, and trustworthy — with a little warmth. We explain, we don't
hype. Confident but humble; we'd rather show the real thing than promise magic.

- **Do:** short sentences, concrete nouns, honest caveats ("needs a model", "best-effort").
- **Don't:** buzzwords, superlatives, exclamation marks, "revolutionary/seamless/magical".

## Taglines

- **Primary:** *All your real context, in one local lake.*
- Real context, on tap, on your machine.
- Stop your AI guessing — give it the whole lake.
- One local lake of context for every AI tool.

## Color palette

Cool and clear, like looking down through still water — light at the surface, deep toward
the bottom, with a single warm "spark" where new context enters.

| Token | Hex | Role |
| --- | --- | --- |
| **Deepwater** | `#0E2A33` | Primary text; dark surface |
| **Lake** | `#137A8B` | Primary brand — headings, links, wordmark |
| **Current** | `#2BB3A3` | Bright accent — the shallows (decorative, not body text) |
| **Mist** | `#EAF4F4` | Light surface / background |
| **Shore** | `#D7C5A0` | Warm neutral — borders, dividers (decorative) |
| **Sun** | `#E7B53C` | Warm accent — the "spark" of fresh context (decorative) |

**Accessibility:** body text is **Deepwater on Mist** (or Mist on Deepwater) — both exceed
WCAG AA. **Lake** is AA on Mist for **headings, links, and large/bold text** (~4.5:1); for
long body copy use Deepwater. **Current, Shore, and Sun are decorative** (fills, accents,
the spark), never body text.

## Typography

All open-source (SIL OFL), so the kit is freely redistributable:

- **Wordmark / display:** **Space Grotesk** (Bold) — sturdy, geometric, a little
  characterful.
- **Body / UI:** **Inter**.
- **Code / CLI:** **JetBrains Mono**.

## Logo & glyph

- **Glyph** ([`glyph.svg`](docs/branding/glyph.svg)): a still lake seen in cross-section —
  depth layers fading from bright shallows to dark deep, a ripple on the surface, and a
  single warm droplet entering — *"new context falling into the pool."* Works as a favicon
  / avatar at small sizes.
- **Wordmark** ([`wordmark.svg`](docs/branding/wordmark.svg)): the glyph + lowercase
  `contextlake` set in Space Grotesk, two-tone (`context` in Deepwater, `lake` in Lake).
  (The SVG uses live text; outline it to paths in a vector tool before shipping a locked
  logo.)

**Usage:** keep clear space of ~1 glyph-height around the logo. Don't recolor outside the
palette, stretch, rotate, add shadows, or place the wordmark on low-contrast backgrounds.

## Mascot

**Pebble**, an otter who **dives into the lake and surfaces holding exactly the context you
need** — the perfect picture of retrieval. See [`docs/branding/mascot.md`](docs/branding/mascot.md)
for the full spec, ASCII sketch, and an image-generation prompt.

## Handles & namespaces

Prefer the bare name **`contextlake`** everywhere it's free (PyPI is confirmed open; do the
GitHub/domain/social sweep before relying on the rest). Where it's already taken, fall back
to **one** consistent house handle rather than a different variant per site:

- **House handle:** **`contextlakehq`** — for X/Twitter and any platform where the bare
  name is unavailable. Use it consistently so the brand is predictable to find.
- **Reddit:** the brand lives in the subreddit **`r/contextlake`** (a separate namespace
  from usernames, so it stays free even when `u/contextlake` is taken).
- **Playful channels** (Discord, Mastodon/Fosstodon, Bluesky): the [mascot](#mascot)'s name
  is an on-brand alternative.

**Where to spend effort** (this is a developer tool): GitHub org · PyPI · npm · Docker Hub ·
Hugging Face · Discord · Mastodon/Fosstodon · Bluesky · Dev.to · X/Twitter · Reddit. Skip
commerce, lifestyle, and defunct platforms — they do nothing for an open-source tool.

## Future scope

When contextlake reaches beyond GitLab (GitHub, Bitbucket, DockerHub, Hugging Face…),
**nothing in this guide needs to change**. Each new source is just another stream feeding
the same lake — the name, palette, voice, and mascot all still fit. That future-proofing is
the whole reason for the name.

## Asset index

- [`docs/branding/glyph.svg`](docs/branding/glyph.svg) — the mark
- [`docs/branding/wordmark.svg`](docs/branding/wordmark.svg) — mark + wordmark
- [`docs/branding/mascot.md`](docs/branding/mascot.md) — mascot spec + sketch + prompt
