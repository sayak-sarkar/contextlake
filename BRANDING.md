# contextlake: Brand Guidelines

**Status:** Final / decision-grade. This document is the single source of truth for the contextlake visual and verbal identity. It supersedes all earlier brand files. Build directly from it into `BRANDING.md`, the `/brand` site page, and image-model prompts.

**Asset status (updated as the system shipped):** `docs/branding/mascot.md` is rewritten to Section 5.
The identity is consolidated on one painterly register (Section 2, 2026-07-28) — the earlier flat
vector mark (`docs/branding/mark.svg`, `mark-dark.svg`, `glyph.svg`, `glyph-mark.svg`) is retired and
kept only for history, not referenced by any current spec or build. `docs/branding/wordmark.svg` is
re-exported single-color (Section 2.4). `docs/branding/mark.png` is now a painterly crop (Section 2.1).

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/branding/mark.png" alt="The contextlake mark: Pebble, a painterly warm-brown otter, cradling a glowing teal context-pebble." width="180">
</p>

---

## 1. Brand essence & voice

### 1.1 Positioning (locked, one line)

**contextlake is the local context layer that lets your AI coding tools answer from your real source instead of their best guess.**

Use verbatim as the repo description, site meta description, and PyPI/ghcr summary. Trim to *"the local context layer that lets AI coding tools answer from your real source"* where a field is short.

- Category noun is **"context layer"**, never "tool", "platform", "framework", or "knowledge base". Always lead with it.
- The villain is named only by implication: **"best guess"** = hallucination. Never use the word "hallucination" in outward hero copy; reserve it for docs and technical explainers.
- **"contextlake" is always one lowercase word.** Never capitalized mid-sentence, never `Context Lake`, never split across a line or by color.

### 1.2 Personality (locked, three words)

**Grounded. Lucid. Warm.**

| Word | Governs | In practice |
|---|---|---|
| **Grounded** | The anti-hallucination promise, "real source", calm authority | Concrete nouns, cited claims; hype verbs ("revolutionize", "supercharge") banned |
| **Lucid** | Clarity, the "surface something clear" payoff, readability | Short sentences. Plain words. One idea per line |
| **Warm** | Pebble, the friendliness, the "instrument that likes you" feel | Second person ("your repos"), light human touches, never cutesy |

If a sentence serves none of the three, cut it.

### 1.3 The metaphor system: a clear lake seen in depth

One controlling metaphor. It is a system, not a one-off pun. Do not invent new aquatic metaphors outside this table.

| Brand concept | Lake term | Allowed use |
|---|---|---|
| The local, unified store of your repos | **the lake** / **your local lake** | "All your context in one lake" |
| Real source being indexed | **what's in the water** / **real source** | "answers from what's actually there" |
| Question → grounded answer | **dive deep, surface something clear** | the core narrative of any explainer |
| A single retrieved, structured answer | **a clear pebble** / **surfaced context** | tied to Pebble + the context-object |
| Pebble (mascot) | the diver who **surfaces** context | "Pebble surfaces the context you need" |
| Indexing / building the graph | **going deep** / **mapping the depths** | onboarding, "how it works" copy |

**Discipline (locked):**
- **"Deep" and "clear" are the two load-bearing words.** Depth = the real, complete source underneath. Clarity = the precise answer back. Every piece of copy points to one or both.
- **Never mix metaphors.** No oceans, waves, currents-as-trends, fishing, drowning-in-data. **Never "data lake"** (reads enterprise-warehouse, not developer-instrument).
- **Depth is positive, never threatening.** The lake is calm and legible to the bottom. Avoid "murky", "deep end", "out of your depth", "sink", "flood".

### 1.4 Tagline hierarchy (locked, four tiers)

1. **Master tagline** (brand-level, pairs with the logo and OG card): **"Deep context. Clear answers."**
2. **Descriptive tagline** (hero subhead / "what it is"): **"All your real context, in one local lake."**
3. **Functional descriptor** (registries, GitHub About, app stores; metaphor-free, searchable): **"A local, offline-first context layer for AI coding tools."**
4. **Proof line** (the anti-hallucination stamp; badges, eyebrows, CTA microcopy): **"Answers from real source."**

**Pairing rule:** Tier 1 + Tier 2 may stack. Never stack Tier 1 with Tier 4 (two slogans compete). Never run more than two of these in one viewport.

### 1.5 The anti-hallucination promise, verbally

- **Canonical phrasing:** *"answers from real source"* (running copy) and *"Answers from real source."* (Tier-4). Approved variants: "grounded in your real source", "from your actual code, not a guess", "reads the source so the model doesn't have to guess".
- **Banned:** "100% accurate", "never wrong", "eliminates hallucinations", "always correct", "guarantees". The promise is **provenance** (the answer is traceable to real source), not **infallibility**. Overclaiming accuracy breaks *Grounded*.
- **Contrast structure** is the workhorse: positive first, negative as a subordinate clause, *"so agents answer from your repos, not from memory."*
- **Show the receipt.** Wherever the product surfaces an answer, the pattern is **claim, then citation** (name the file/repo). "From `core.py`" out-persuades any adjective. This is the single most on-brand verbal behavior the product can perform.

### 1.6 Voice: do / don't

**Hero / landing**
- ✅ "Your AI assistant is guessing about your code. contextlake gives it the real thing: a local map of your repos it can actually read."
- ❌ "Unleash next-gen AI superpowers and revolutionize your workflow with cutting-edge context intelligence."

**Feature / docs**
- ✅ "contextlake indexes your repos into a queryable graph and serves it to your editor over MCP. Ask a cross-repo question; get an answer with the files it came from."
- ❌ "Leveraging a sophisticated knowledge-graph architecture, contextlake synergizes your codebase into an intelligent, queryable substrate."

**CTA / microcopy**
- ✅ "Point contextlake at your repos and let Pebble surface the context. Takes one command."
- ❌ "Get Started Now and Transform Your Development Experience Today!!!"

**Checklist (apply to any new copy):**
- Sentence case for everything except the wordmark and proper nouns. The wordmark is always lowercase `contextlake`.
- Second person. The reader owns the lake.
- One claim, one line. If you wrote "leverage", "seamless", "powerful", "revolutionary", "supercharge", "next-gen", "robust", or "intelligence" (as a noun), rewrite.
- Prefer a cited noun over an adjective.
- **No em-dashes anywhere** (use a period, comma, colon, or parentheses). No exclamation points in body copy (one allowed, rarely, in the warmest microcopy).

---

## 2. Logo & mark system

**Consolidated to one register (2026-07-28).** contextlake previously ran a dual-element identity: a
flat, front-facing vector otter-head mark (fur `#2F5A63`, deepwater keyline) alongside the painterly
mascot. That flat mark is **retired** — it was a separate, incorrectly-colored design (a cool
blue-grey/slate fill that never matched the shipped painterly art) rather than a deliberate second
register, and it is no longer used anywhere. The identity is now **one Pebble, rendered painterly, at
every size the format allows**, plus the **`contextlake` wordmark** in live Space Grotesk.

The retired flat-mark assets (`docs/branding/mark.svg`, `mark-dark.svg`, `glyph.svg`, `glyph-mark.svg`,
`pebble-logo-flat.png`, `pebble-logo-flat-cut.png`) are kept in the repo for history but are not
referenced by any build, page, or spec — do not resurrect them as "the logo."

### 2.1 The mark

The mark is a painterly Pebble crop: front-facing or three-quarter head-and-shoulders, cradling the
context-pebble, on the standard palette background (rounded-square container, solid **deepwater
`#0E2A33`**, never transparent — a transparent mark vanishes on dark browser tab strips and dark UI
chrome). Source: the same painterly generation pipeline as the mascot (§5), cropped/composed for a
square or near-square frame. Current canonical source asset: `docs/img/icon-512.png`; the small
nav/footer embed (`docs/branding/mark.png`) is a resize of the same source, not a separate artwork.

**One exception, for practical legibility, not aesthetics:** below about 48px, painterly shading
degrades to a muddy blob (fine gradients and soft edges don't survive that much downsampling). At
**16–32px only** (browser favicon, tab strip), contextlake ships a **simplified silhouette** derived
from Pebble's outline — solid shapes, no gradients, the pebble rendered as a single glowing dot — so
the mark stays legible instead of turning into noise. This is a rendering necessity, not a second
brand register: it's a simplified silhouette of the one painterly character, not an independent flat
logo with its own construction rules or color system. Built by `site/tools/gen_small_mark.py`; see §2.3.

### 2.2 The context-pebble inside the mark

Same object as the painterly mascot's context-pebble (§5.4): translucent, glowing, teal-to-lake falloff
with a single gold glint, cradled in both forepaws. At mark scale it may simplify (fewer internal
gradient steps) but never changes shape, color family, or the "one glint" rule. No facet lines,
star-dots, or node-graph inside it at any scale.

### 2.3 Small-size favicon simplification (16–32px only)

- **Container:** rounded-square (iOS squircle), corner radius ≈ 22% of edge, solid **deepwater
  `#0E2A33`** background (never pure black, never transparent).
- **Silhouette:** a simplified Pebble outline in a light, brand-palette tone against the deepwater
  field, with the context-pebble as a single glowing dot (current `#2BB3A3` → sun `#E7B53C` accent).
  No fur texture, no shading gradient, no facets — legibility at 16px is the only goal.
- Ship layered: 16/32/48/64px (this simplified tier) plus 180/192/512px (the full painterly crop,
  §2.1) and a maskable 512 with 10% safe padding (Android adaptive icons).
- The favicon never renders a legible knowledge-graph; graph semantics live only in large illustration
  and product UI.

### 2.4 Wordmark

- **Typeface:** Space Grotesk, **Bold (700)**.
- **Casing:** all-lowercase single word **`contextlake`**. Never split, camel-cased, or wrapped.
- **Color: single color.** deepwater `#0E2A33` on light; mist `#EAF4F4` on dark. **This overrides the shipped two-tone split**, one word, one color, always.
- **Tracking:** **−1.5% (−15/1000 em)** at display sizes ≥ 24px cap. For UI use < 16px cap, reset tracking to **0** to preserve letterfit.
- **Reproduction:** always typeset live from Space Grotesk Bold. **Never** redraw, outline-warp, or AI-generate the lettering. Ship an outlined-vector copy only for environments without font embedding.
- Default OpenType features only; the single-story `a`/`t` are equity, do not substitute stylistic sets.

### 2.5 Lockups

Mark bounding-box height = **"M"**.

- **A. Horizontal (primary).** Mark left, wordmark right, vertically centered on the mark. Gap between
  mark and first glyph ≈ **0.5M**. Use: site header, README banner, PyPI, wide social profile.
- **B. Stacked (secondary).** Mark centered above, wordmark centered below, width ≤ **1.6M**. Use:
  square/portrait, splash, sticker, docs hero.
- **Mark-only** (avatar/favicon) and **wordmark-only** (inline in nav/body) are both sanctioned standalone.

**Locked:** never swap the arrangement (no wordmark-left) and never invent lockups beyond these.

### 2.6 Clear space

Clear space ≈ **20% of mark height** on all four sides, measured from the outermost ink of whichever
element is present. Nothing (type, rule, edge, other logo) enters this zone.

### 2.7 Minimum sizes

| Asset | Digital min |
|---|---|
| Horizontal lockup | **120 px** wide |
| Stacked lockup | **96 px** wide |
| Wordmark alone | **84 px** wide |
| Mark alone, full painterly crop | **48 px** |
| Mark alone, simplified favicon tier (§2.3) | **16 px** |

Below the horizontal/stacked minimums, drop the wordmark and show mark-only.

### 2.8 Misuse (do not)

1. Recolor Pebble or the pebble outside the palette in §3/§5.
2. Use pure-black `#000000` anywhere; deepwater `#0E2A33` is the darkest allowed value.
3. Place a node-graph, star-dots, or facets inside the pebble at any scale.
4. Add water droplets/sweat to the face, or redirect painterly gaze off-page (§5.6).
5. Rotate, skew, stretch, condense, or outline-warp the mark or wordmark.
6. Re-typeset the wordmark in any font but Space Grotesk Bold, or change its lowercase one-word casing.
7. Split `contextlake` into two words, colors, or lines.
8. AI-generate or hand-redraw the wordmark lettering.
9. Violate clear space (§2.6) or go below the §2.7 minimums.
10. Reintroduce a flat vector mark as an alternate "logo register" — retired per §2, not to be revived.
11. Swap the lockup arrangement or invent lockups beyond §2.5.

---

## 3. Color system

The system is **dark-first** with a fully paired light theme. Tokens are namespaced `--cl-*`. Every pair below is contrast-tested against WCAG 2.2; **the stated ratios are the source of truth, not the swatches.**

### 3.1 Foundational rule: no pure black, no pure white surface

`#000000` is **forbidden everywhere**, backgrounds, text, borders, shadows, icon strokes, keylines. The darkest tone in the light theme is **deepwater `#0E2A33`**; the moonlit-navy dark theme goes a touch deeper on recessed panels (`#081D30`, §3.4). Shadows are deepwater at low alpha (`rgba(14,42,33,0.X)`), never black. `#FFFFFF` is permitted only as a light-theme **card surface** and as on-fill text over lake; it is never a page background (light canvas is mist-tinted `#F3F8F8`).

### 3.2 Token taxonomy (three tiers: replaces the "six locked" framing)

The brand runs more than six values, and pretending otherwise reads as sloppy. The truth is a **3-tier system, all under `--cl-*`**:

1. **Brand primitives, the locked six.** The governing identity. Everything else derives from these.
2. **Derived UI ramp.** Surfaces, borders, muted/subtle text, and functional status (success/warn/error), algorithmically derived from the primitives and contrast-locked. Used in product/site chrome.
3. **Illustration / mascot ramp.** The fur set + pebble-glow falloff (§5.3–§5.4). Used **only** in painterly art, **never** in UI.

The six primitives stay the governing identity; tiers 2 and 3 are sanctioned, named extensions, not stray colors.

### 3.3 Tier 1: brand primitives (locked six)

| Primitive | Hex | Role |
|---|---|---|
| deepwater | `#0E2A33` | the darkest tone in the system; canvas dark, keyline, text on light |
| lake | `#137A8B` | structural blue-teal; fills (with white text), the "second brand signal" |
| current | `#2BB3A3` | the interactive teal-green; dark-theme brand/link, glow |
| mist | `#EAF4F4` | the light tone; text on dark, light sunken surface |
| shore | `#D7C5A0` | **decorative warm-neutral, illustration only** (sand/light, painterly dividers). Never a UI text/fill color; never a page background. |
| sun / gold | `#E7B53C` | decorative + emphatic accent; never functional text on light |

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/brand-palette.png" alt="The six locked brand primitives as swatches: deepwater #0E2A33, lake #137A8B, current #2BB3A3, mist #EAF4F4, shore #D7C5A0, and sun #E7B53C." width="820">
</p>

### 3.4 Tier 2: dark theme (moonlit-navy) semantic roles

The dark theme is a **moonlit-navy** night lake, cooler and bluer than the deepwater light-mode tone, so the dark UI coheres with the moonlit-night hero (light theme = sunrise, dark = moonlight). **`deepwater #0E2A33` stays a locked primitive and the light-mode dark tone**; the dark canvas is a navy derivation of it. (Sampled from the night hero; see §6.)

| Role | Token | Hex | Verified |
|---|---|---|---|
| Canvas bg | `--cl-bg` | `#0C2438` | text mist **14.15:1** |
| Surface (card) | `--cl-surface` | `#15314C` | text mist **11.90:1** |
| Surface raised | `--cl-surface-raised` | `#18344F` | text mist **11.41:1** (subtle 4.75) |
| Recessed panel | `--cl-panel` | `#081D30` | bands/footers/code blocks; text mist **15.26:1** |
| Text | `--cl-text` | `#EAF4F4` | 14.15:1 on bg ✓ AAA |
| Muted text | `--cl-text-muted` | `#A4B3B6` | **7.32:1** bg ✓ (≥5.5 all surfaces) |
| Subtle text | `--cl-text-subtle` | `#8FA1A5` | **≥4.5:1 on bg, surface, and raised** ✓ |
| Brand / link | `--cl-brand` | `#2BB3A3` | **6.10:1** on bg ✓ (always underlined) |
| Accent fill | `--cl-accent` | `#137A8B` (lake) | fill only; **on-text = mist/white (5.02)** |
| Border interactive | `--cl-border` | `#728990` | **≥3:1 on bg, surface, and raised** ✓ (1.4.11) |
| Divider (decorative) | `--cl-line` | `#274864` | exempt |
| Focus ring | `--cl-focus` | dual-tone (§3.7) | ≥3:1 vs component **and** surface |
| Success | `--cl-success` | `#3DD9A0` | **8.78:1** on bg ✓ |
| Warning | `--cl-warn` | `#E7B53C` (sun) | icon/large + **deepwater label**; 8.36:1 on bg |
| Error | `--cl-error` | `#FF8388` | **6.68:1** on bg ✓ (≥4.5 on surface/raised) |

The **light theme (§3.5) is unchanged** and keeps deepwater as its dark tone. Teal accents (`current`, `lake`) are kept in both themes for pop. *Product surfaces (dashboard, graph UI) should adopt this same moonlit-navy dark ramp for cross-surface consistency.*

### 3.5 Tier 2: light theme semantic roles

| Role | Token | Hex | Verified |
|---|---|---|---|
| Canvas bg | `--cl-bg` | `#F3F8F8` | text deepwater **14.02:1** |
| Surface (card) | `--cl-surface` | `#FFFFFF` | text deepwater **15.03:1** |
| Surface sunken | `--cl-surface-sunken` | `#EAF4F4` (mist) | text deepwater **13.41:1** |
| Text | `--cl-text` | `#0E2A33` | 14.02:1 on bg ✓ AAA |
| Muted text | `--cl-text-muted` | `#566B70` | **5.25:1** bg ✓ (≥4.76 all light surfaces) |
| Subtle text | `--cl-text-subtle` | `#5E777C` | corrected; ≥4.5:1 on bg & surface ✓ |
| Brand / link | `--cl-brand` | `#0E6675` | **6.17:1** bg / 5.90:1 sunken ✓ (always underlined) |
| Accent fill | `--cl-accent` | `#137A8B` (lake) | fill only; on-text white **5.02:1** ✓ |
| Border interactive | `--cl-border` | `#788C90` | corrected; **≥3:1 on bg, surface, and sunken** ✓ |
| Divider (decorative) | `--cl-line` | `#D2E0E0` | exempt |
| Focus ring | `--cl-focus` | dual-tone (§3.7) | ≥3:1 vs component **and** surface |
| Success | `--cl-success` | `#147A53` | **5.33:1** on surface ✓ |
| Warning (text) | `--cl-warn` | `#8A6000` | corrected; **≥4.5:1 on bg, surface, and sunken** ✓ |
| Error | `--cl-error` | `#C2363B` | **5.41:1** on surface ✓ |

> Light-theme brand/warn/error/success use darkened derivations of the palette family because the raw primitives (`current`, `lake`, `sun`) fail as light-surface text. Hues stay in their locked families. `#8A6000` is amber-for-warn-text only, the single warm value introduced beyond the six, quarantined to the warn role because no locked color yields an AA label on a light surface.

### 3.6 The on-fill law (locked: corrects the earlier 8.86 error)

Text/icon color on a colored fill is fixed, in **both** themes:

- **On `lake #137A8B` → mist/white** (`5.02:1` ✓). **Never deepwater** (deepwater on lake = **2.99:1**, a fail).
- **On `current #2BB3A3` → deepwater** (`5.78:1` ✓). **Never white** (white on current = 2.60, fail).
- **On `sun #E7B53C` → deepwater** (high contrast ✓). **Never white** (white on sun = 1.90, fail).

**Mnemonic:** *lake takes white; current and sun take deepwater.*

### 3.7 Focus ring (WCAG 2.2 SC 2.4.13)

A single-hue teal ring on a teal/current button is ~1:1 and disappears. Locked focus geometry:

- **2px solid ring + 2px offset gap**, dual-tone so it clears ≥3:1 against **both** the component and the adjacent surface:
  - **Light:** deepwater core + mist outer.
  - **Dark:** mist core + deepwater outer.
- **SC 2.4.11 (Focus Not Obscured):** the peeking-Pebble corner accent and any sticky header must never overlap a focused control.

### 3.8 Buttons & gradients

- **Primary button (text-bearing):** **solid `lake #137A8B` fill + white/mist text (5.02:1 ✓)**, both themes. Do not put a label on the lake→current gradient.
- `--cl-grad-cross-section` (decorative "lake seen in depth" column, hero bands/divider): `linear-gradient(180deg, #EAF4F4 0%, #2BB3A3 22%, #137A8B 58%, #0E2A33 100%)`. Text only over the bottom (deepwater) third, in mist.
- `--cl-grad-depth` (quiet dark section backdrop): `linear-gradient(180deg, #16323B 0%, #0E2A33 100%)`. Mist body text passes throughout (≥10.6:1).
- `--cl-grad-brand` (**non-text decorative fills only**, chips, glow, accents): `linear-gradient(135deg, #137A8B 0%, #2BB3A3 100%)`. **Never carries a label** (no single text color passes across the sweep: deepwater fails the lake end at 2.99, white fails the current end at 2.60).
- `--cl-glint-gold` (decorative spark only, never beneath content): `radial-gradient(circle, rgba(231,181,60,0.50) 0%, rgba(231,181,60,0) 70%)`.

### 3.9 Gold discipline (locked)

Gold `#E7B53C` is **decorative and emphatic, never functional text on light, never a link, never the sole carrier of meaning.**

- **Permitted:** the pebble's inner glint; a single hairline keyline accent; an active-tab or "live/fresh" indicator dot; sparkle/glow in illustration; large display numerals **on dark** (`7.92:1` ✓).
- **Forbidden:** gold body copy on light (gold/mist **1.69:1**, gold/white **1.90:1**, gold on `#F3F8F8` **1.77:1**, all fail); gold links; gold form borders; gold as a status color; more than **one** gold accent per viewport region.
- On dark, gold may label icons/large text (≥7.9:1); pair it with deepwater when it becomes a fill.

### 3.10 Enforcement (CI-checkable)

1. Reject any `#000000`/`black`/`rgb(0,0,0)`; darkest allowed is `#0E2A33`.
2. Components reference **semantic `--cl-*` tokens only**, no raw primitive hex, no off-token values, in component CSS.
3. Body text (<18px / <14px bold) requires **≥4.5:1**; large text and UI borders/focus require **≥3:1**. The tables above are the allow-list; any new pairing is re-tested, not eyeballed.
4. Lint the inverse pairs: `current`/`sun` never carry white text; `lake` never carries deepwater text; gold never carries functional text on light.
5. **`--cl-text-subtle`/`--cl-border`** are valid only at their verified-surface scopes; lint to forbid out-of-scope use.

---

## 4. Typography

Three-typeface system. The split is functional. Never add a fourth family; never set body in Space Grotesk or headings in the mono.

### 4.1 Roles (locked)

| Role | Typeface | Used for |
|---|---|---|
| Display / headings | **Space Grotesk** | Hero display, H1–H4, eyebrows/labels, pull quotes, stat numerals, nav/button labels, the wordmark |
| Body / UI | **Inter** | Paragraphs, lists, captions, forms, tooltips, table cells, most chrome |
| Code / literal | **JetBrains Mono** | CLI commands, file paths, code blocks, MCP payloads, repo/branch names, version tags, key bindings |

**Monospace lock:** **JetBrains Mono** (SIL OFL, self-hostable; tall x-height, slashed zero, disambiguated `1 l I` / `0 O`). Sanctioned alternative only if a build-size budget forces it: **IBM Plex Mono**.

**Display-vs-body rule:** Space Grotesk above 20px and for any caps label; Inter at/below 18px for reading text. The only Inter use above 18px is the 18px lead paragraph. The only Space Grotesk use below 20px is the eyebrow (12–14px) and nav/button labels.

### 4.2 Modular scale (base 16px = 1rem, ratio 1.25)

| Token | Face | px / rem | Weight | Line-height | Tracking |
|---|---|---|---|---|---|
| `--type-display` | Space Grotesk | 60 / 3.75 | 600 | 1.05 | −0.02em |
| `--type-h1` | Space Grotesk | 44 / 2.75 | 600 | 1.10 | −0.02em |
| `--type-h2` | Space Grotesk | 32 / 2.00 | 600 | 1.15 | −0.015em |
| `--type-h3` | Space Grotesk | 24 / 1.50 | 600 | 1.25 | −0.01em |
| `--type-h4` | Space Grotesk | 20 / 1.25 | 500 | 1.30 | 0 |
| `--type-lead` | Inter | 18 / 1.125 | 400 | 1.60 | 0 |
| `--type-body` | Inter | 16 / 1.00 | 400 | 1.60 | 0 |
| `--type-small` | Inter | 14 / 0.875 | 400 | 1.50 | 0 |
| `--type-caption` | Inter | 12 / 0.75 | 500 | 1.40 | +0.01em |
| `--type-eyebrow` | Space Grotesk | 13 / 0.8125 | 600 | 1.20 | +0.08em **(UPPERCASE)** |
| `--type-mono` | JetBrains Mono | 14 / 0.875 | 400 | 1.55 | 0 |
| `--type-mono-block` | JetBrains Mono | 14 / 0.875 | 400 | 1.65 | 0 |

Body emphasis uses Inter 600 (not 700). Stat/numeric displays use Space Grotesk 600 with **tabular figures** (`font-feature-settings: "tnum" 1`).

**Eyebrow casing (reconciled):** the sentence-case voice rule (§1.6) governs *running copy*. The **eyebrow/label is an exempt typographic device**, UPPERCASE at +0.08em is permitted and locked for `--type-eyebrow` only. All-caps is never used for sentences or paragraphs.

### 4.3 Weights (ship only these: eight files)

- **Space Grotesk:** 500, 600, 700 (700 reserved for the wordmark + rare emphasis).
- **Inter:** 400, 500, 600, plus **Inter italic 400** (inline emphasis/citations only).
- **JetBrains Mono:** 400, 500.

No light (<400) weights anywhere (they fail contrast on dark surfaces). No Space Grotesk or mono italics.

### 4.4 Letter-spacing & optical rules

- Space Grotesk runs wide; **tighten as size grows** (negative tracking on all headings ≥24px). No positive tracking except the eyebrow.
- Inter: **zero tracking** at body sizes. Never letter-space running text.
- Enable Inter `calt`. **JetBrains Mono ligatures OFF by default** in product UI (so `=>`, `!=` read literally); ligatures may be on in marketing code samples only.

### 4.5 Web-font loading (offline-first, hard constraint)

The shipped product fetches **no external runtime assets**, fonts are **self-hosted, never Google Fonts/CDN**.

1. Bundle **subset WOFF2** (Latin + Latin-Ext, subset to glyphs used). WOFF2 only.
2. `@font-face` with `font-display: swap` on body/mono, `font-display: optional` on the display face (avoids a hero reflow).
3. `<link rel="preload" as="font" type="font/woff2" crossorigin>` only the three above-the-fold faces: Space Grotesk 600, Inter 400, JetBrains Mono 400.
4. Define `size-adjust`/`ascent-override` on fallback aliases to keep CLS ≈ 0 during swap.

```css
--font-display: "Space Grotesk", ui-sans-serif, system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
--font-body:    "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
--font-mono:    "JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", "Cascadia Code", Menlo, Consolas, "Liberation Mono", monospace;
```

### 4.6 Accessibility minimums (WCAG 2.2 AA)

- Body reading text never below **16px**; absolute floor **12px**; mono inline/blocks **14px** min.
- Body line-height ≥ **1.5** (SC 1.4.12); headings may go to 1.05.
- Measure **60–75 characters**; cap content columns at ~`68ch`.
- Contrast: normal text ≥ **4.5:1**, large text (≥24px or ≥18.66px bold) ≥ **3:1**. Use the §3 token pairs.
- **Links:** light `--cl-brand #0E6675` + underline; dark `current #2BB3A3` + underline. Never color-alone, pair with underline (links), an icon, or a label.
- `current`/`lake`/`gold` are **never running body text on light**.
- Layout survives 200% zoom and 400% reflow without horizontal scroll; all sizes in `rem`. Honor `prefers-reduced-motion` for any text reveal.

---

## 5. Mascot & the context-pebble

This section is canon. Pebble and the context-pebble are fixed; their silhouette, proportions, palette, and locked phrases are not re-invented per asset.

### 5.1 Who Pebble is

Pebble is a small, friendly, innocent-faced **warm brown river otter** who surfaces from the contextlake cradling a glowing translucent **context-pebble** in both forepaws, offering it forward. Read in one beat: *"I went down, I found the real thing, here it is."* Calm, competent, never frantic. Pebble is the embodiment of the anti-hallucination promise, it returns with real context, not a guess.

**A note on "brown," measured, not eyeballed (2026-07-28):** sampling actual pixels from shipped art
(`docs/img/pebble-peek.png`, `docs/img/icon-512.png`) shows the crown/back sit in the scene's ambient
cool lake-light and read as a dark, cool-cast charcoal in shadow (`#1C2D33`–`#2A393E`) — that's
**lighting, not fur color**. The muzzle, cheeks, and paws, which catch more direct light, read as clearly
warm tan/cream (`#D9CA99`–`#FBE7AB`), and mid-tone fur in better light reads as genuine warm umber-brown
(`~#A0876A`). Pebble's fur is brown; do not flatten the whole body to one cool-grey value the way the
now-retired flat mark did (§2) — that drift is exactly how this doc ended up saying "dark blue-grey" in
the first place. See §5.3 for the corrected ramp.

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/branding/pebble-doc.png" alt="Pebble, contextlake's mascot: a small, friendly warm brown river otter cradling a glowing translucent context-pebble in both forepaws, offering it forward." width="240">
</p>

### 5.2 Silhouette & proportions (locked)

Recognizable as a **solid rounded silhouette at 10% size**, the pebble glow as the single brightest point.

- **Build:** rounded, buoyant, river-smoothed; soft pebble-like mass, no sharp anatomy. Polished stone, not realistic mustelid.
- **Head : body ratio = 1 : 1.6** (deliberately juvenile/cute; a realistic adult would be ~1:3).
- **Head:** broad rounded cranium; short muzzle (≤ **35%** of head width); small rounded ears set wide and low.
- **Eyes:** large, round, dark, **~40%** down the face, inter-eye gap ≈ one eye-width. A single soft catch-light (upper-left), no second sparkle. Open and soft, never half-lidded or wide-ring.
- **Nose:** small rounded heart/triangle, deepwater-toned.
- **Forepaws:** small, 4-digit, always **cradling the pebble from below**, cup, never grip.
- **Tail:** thick tapered, a seated stabilizer; never focal.
- **Whiskers:** 2–3 fine low-contrast strokes per side; optional, omit at small sizes.

### 5.3 Fur & body color (illustration ramp: measured from shipped art, corrected 2026-07-28)

| Zone | Hex | Role |
|---|---|---|
| Crown, back, tail (in the scene's ambient cool light) | `#1C2D33` – `#2A393E` | darkest fur value; a cool-cast **shadow**, not the material color (§5.1) |
| Body mid-tone, better-lit fur | `~#A0876A` | the true warm umber-brown read |
| Muzzle, cheeks, chin, paws | `#D9CA99` – `#FBE7AB` | warm cream-tan, the lightest fur |
| Nose | `#0E2A33` (deepwater) | the darkest allowed point |
| Eye | `#0E2A33` iris + single `#EAF4F4` catch-light | n/a |

Wet-fur sheen is soft `#EAF4F4` painterly highlights at low opacity, **never discrete water droplets**.
Do not render the crown/back as a flat, uniform slate/teal fill — that was the retired flat mark's error
(§2): the cool cast belongs to shadow modeling under the brand's lake-light, layered over warm-brown fur,
not a fur color in its own right.

### 5.4 The context-pebble (locked, the most controlled element)

- **Material:** smooth polished **translucent agate / sea-glass**, lit from within.
- **Shape:** rounded organic pebble, slightly taller than wide. **No facet edges that read as a cut gem.** Faint internal striations may *hint* at structure but stay soft and subsurface.
- **Inner glow (identical in both registers):** **core `current #2BB3A3` → falloff to `lake #137A8B` → thin `mist #EAF4F4` rim** where it meets dark fur. The glow is the brightest value in any composition.
- **Gold glint:** exactly **one** small warm highlight in **sun `#E7B53C`** near the upper third (one point or short streak). One glint, never a constellation.
- **Cast glow:** the pebble casts soft teal light onto Pebble's paws/chest/chin, proof it really glows.
- **Scale:** pebble diameter ≈ **45–55%** of head width (co-subject, never larger than the head) at
  mascot scale; the mark (§2.1) and its 16–32px favicon simplification (§2.3) may shrink it further for
  legibility, same object, smaller reference frame.

**Hard pebble rule:** NOT an amber sphere with star-dots (Dragon Ball), NOT a faceted crystal/gem, NOT a gold nugget, NOT a node-graph inside. The "knowledge graph" semantic lives in large illustration and product UI only, never inside the carried pebble.

### 5.5 Expression range (the only approved set)

1. **Calm-warm (default):** soft open eyes, gentle smile, relaxed brow.
2. **Quietly proud / presenting:** default + subtle upward chin + brighter eye catch-light, at a CTA.
3. **Curious-focused:** eyes slightly more open, looking toward the content/feature.
4. **Friendly-delight (sparing):** a slightly bigger smile for success states.

**Out of canon:** anxious, sweating, surprised, angry, sad, sleepy, winking, tongue-out, or any overacting. Pebble is friendly-but-precise.

### 5.6 Poses / personas (locked set)

- **Presenting (hero/primary):** front-facing, seated/surfacing at the waterline, both paws cradling the pebble forward. The canonical pose.
- **Diving / snorkel ("going to get context"):** swim/dive pose with a small snorkel, descending. For fetching/indexing/working states. Pebble may be empty-pawed here.
- **Surfacing (loading→success):** breaking the surface, pebble first catching its glow. Pairs with diving.
- **Peeking / corner accent:** peeking from a screen edge, gaze turned inward. Strictly small, decorative.
- **Resting-on-pebble (idle/empty states):** seated beside/leaning on the pebble at rest, glow dimmed.

**Gaze rule:** in every pose, **gaze points toward the content, the CTA, or the pebble, never off-page
or viewer-locked** — including the mark (§2.1), which is a cropped painterly pose, not a separate logo
convention.

### 5.7 Inverse-density rule (how much Pebble)

Pebble's presence is **inversely proportional to surface information-density.**

- **High-density (graph visualizer, docs, dashboards, code/MCP output):** Pebble **absent or a ≤32px corner/empty-state accent only.** The data is the hero; Pebble never competes with a live graph.
- **Medium-density (landing sections, feature cards, onboarding):** one Pebble per viewport max, supporting.
- **Low-density (hero, OG/social, 404/empty, splash):** Pebble may be the painterly hero at full richness.

**One Pebble per view, never two.** Pebble is punctuation, not wallpaper. If a screen already has a bright focal point, Pebble steps back.

### 5.8 One register, one exception

Pebble renders **painterly** everywhere: rich, dimensional, subsurface glow, soft volumetric light, no
hard black outlines, forms separated by value and rim light. Hero, OG/social, large illustration,
onboarding, the mark (§2.1), app icons, README banners — all the same rendering.

The **only** exception is the 16–32px favicon tier (§2.3), a simplified silhouette used purely because
painterly shading doesn't survive that much downsampling — a technical necessity, not a second brand
register. There is no separate "flat mark" style to keep in sync; there is one Pebble and one small-size
simplification of it.

### 5.9 Hard do / don'ts

**Do:** keep the pebble glow the single brightest point; cast its teal glow onto Pebble; aim gaze at
content/CTA/pebble; use deepwater `#0E2A33` as the darkest value; keep Pebble outline-free.

**Don't:** ❌ node-graph/star-dots inside the pebble · ❌ amber sphere, faceted gem, or gold-nugget pebble · ❌ water droplets/sweat on the face · ❌ hard black outlines on painterly art; no `#000` anywhere · ❌ off-page or viewer-locked painterly stare · ❌ more than one gold glint; no second eye-sparkle · ❌ anxious/shocked/angry/sleepy/silly expressions · ❌ two-Pebble compositions; no Pebble over a live graph · ❌ AI-generated wordmark.

### 5.10 Prompt-ready generation guidance

Use the base prompt + locked phrase blocks. Keep locked phrases verbatim; vary only **[POSE]** and **[CONTEXT]**.

**Reusable base prompt**
> Mascot illustration of **Pebble**, a cute innocent-faced **warm brown river otter** with a rounded river-smoothed body, broad head (head-to-body ratio 1:1.6), short muzzle, large soft dark eyes with a single upper-left catch-light, small rounded ears. Umber-brown fur (`~#A0876A` mid-tone), warm cream-tan muzzle/cheeks/paws (`#D9CA99`–`#FBE7AB`); the crown and back may fall into a cool-shadowed charcoal (`#1C2D33`–`#2A393E`) under the scene's ambient lake-light — that's lighting, never a flat slate/grey fur color. Deepwater `#0E2A33` nose, no pure black. Pebble is **[POSE]**, cradling in both forepaws a **smooth polished translucent agate sea-glass context-pebble with a cool teal-green inner glow (`#2BB3A3` core falling off to lake `#137A8B`) and exactly one small warm-gold glint (`#E7B53C`)**; the pebble is lit from within and casts a soft teal glow onto Pebble's paws and chin. Setting: a calm misty lake at the waterline, **[CONTEXT]**. Calm, trustworthy, friendly-but-precise mood. Rich dimensional painterly illustration, soft volumetric subsurface lighting, gentle rim light in mist `#EAF4F4`, NO hard outlines, forms separated by value and color, soft depth-of-field lake background. Gaze directed toward the context-pebble / the viewer's content. Palette: deepwater `#0E2A33`, lake `#137A8B`, current `#2BB3A3`, mist `#EAF4F4`, shore `#D7C5A0`, gold `#E7B53C`.

**Locked phrases (paste verbatim):**
- `cute innocent-faced warm brown river otter`
- `smooth polished translucent agate sea-glass context-pebble`
- `cool teal-green inner glow (#2BB3A3 core to lake #137A8B falloff) and exactly one small warm-gold glint (#E7B53C)`
- `lit from within, casts a soft teal glow onto Pebble's paws and chin`
- `no pure black, deepwater #0E2A33 is the darkest tone`
- `gaze toward the context / content`

**Mandatory negative prompt (always include):**
> amber sphere, glowing orb with star dots, dragon ball, faceted crystal, cut gem, gold nugget, network graph inside the stone, glowing nodes inside, water droplets on face, sweat drops, tears, anxious expression, shocked face, angry, sleepy, hard black outlines, pure black, neon, text, watermark, two otters, realistic otter anatomy, off-page stare, flat uniform slate/blue-grey fur.

**Slot examples**
- Hero: `[POSE]` = *surfacing at the waterline, presenting the pebble forward* · `[CONTEXT]` = *soft sunrise mist, calm reflective water*.
- Loading/working: `[POSE]` = *diving downward wearing a small snorkel* · `[CONTEXT]` = *deeper teal underwater light*. (Pebble may be empty-pawed.)
- Mark / app icon: `[POSE]` = *front-facing head-and-shoulders only, pebble centered below the chin* · `[CONTEXT]` = *tight crop, solid deepwater background*.

**Wordmark production law (locked):** any asset containing "contextlake" is a **two-step composite**, generate the painterly background **without text**, then overlay the wordmark/tagline in real Space Grotesk. **Never ship a single AI generation with baked-in lettering.**

---

## 6. Illustration, imagery & motion

### 6.1 One register, and where the 16–32px exception applies

Pebble is painterly everywhere: mascot poses, hero, OG/social, large spot illustration, docs section
headers, the mark, app icons, README badges. Rendering is soft volumetric shading (2–4 light planes),
subsurface pebble glow, atmospheric depth, painterly edges, no outline (form reads by value/light).
Format: PNG → WebP/AVIF.

**The one exception:** below about 48px (browser favicon tab-strip sizes, 16–32px), painterly shading
turns to mud on downsample. That tier alone uses the simplified silhouette from §2.3 — solid shapes, no
gradient, corner radius ≥ 12% of shape width, SVG/PNG. It shares the same pebble-glow palette
(`#2BB3A3` core → `#137A8B` falloff → mist rim, one `#E7B53C` glint) so it still reads as the same
character, just simplified for legibility, not a second illustration style to maintain in parallel.

### 6.2 Scene composition: the layered Pebble technique

The signature image is **"Pebble surfacing in the lake,"** built as discrete layers so the mascot moves independently and never carries baked-in water on its body.

**Layer stack (back → front):**
1. **Scene background**, painterly misty lake, full-bleed. Deepwater `#0E2A33` floor → lake `#137A8B` mid → mist `#EAF4F4` horizon. Darkest pixel = deepwater.
2. **Atmosphere**, far mist band + depth haze, 20–40% opacity, blurred.
3. **Waterline**, the surface ripple/meniscus band; the mask boundary, the only place water touches Pebble. Overlaps the lower 18–25% of Pebble's body to seat it.
4. **Pebble (water-less layer)**, transparent-PNG Pebble drawn **dry**; layer 3 sits in front of its lower body to fake submersion. This lets Pebble bob and dive without dragging painted water.
5. **Pebble-orb bloom**, additive teal-gold light bloom, screen blend, 30–60% opacity.
6. **Foreground + UI**, CTA, wordmark, copy.

**Placement:** rule-of-thirds. Pebble's head on an upper third intersection; the cradled pebble as close to optical center as possible, on the axis toward the primary CTA. Gaze vector runs eyes → pebble → CTA. Keep ≥8% canvas-width clear margin around the silhouette.

**Reusability:** ship the scene background and the Pebble layer as **separate files**. New scenes recompose these two plus a fresh waterline, never a fused re-render.

### 6.3 The "alive" hero: motion spec

Motion is **reduced-motion-first**: the static composition is canonical; motion is progressive enhancement. Animate **only transform/opacity** (GPU-cheap, no reflow). First paint renders the static pose immediately; motion attaches after hydration (never a frame of empty hero).

**6.3a Idle bob (default, always-on unless reduced-motion):** Pebble + orb bloom `translateY` sine loop, amplitude **6px**, period **4.5s**, `ease-in-out`, infinite. Orb opacity breathes **0.45 → 0.6 → 0.45** on the same period. Waterline micro counter-bob (amplitude 2px, +180° phase) so the meniscus holds still relative to the otter.

**6.3b Cursor parallax (pointer devices only):** pointer → Pebble offset, gain **0.018**, clamped **±8px X / ±5px Y**. Background parallaxes opposite at **0.4×**. Lerp toward target at **0.08/frame**. Disabled when `(pointer: coarse)`.

**6.3c Staged dive (the delight moment):** triggered on (a) idle 12s, or (b) hero scrolled out the viewport top. **One dive then resurface; never loop.**
- Anticipation (0–250ms): rise `translateY -10px` + `scaleY 1.03`, `ease-out`.
- Plunge (250–900ms): `translateY +140px`, `scaleY 0.92` squash at the waterline, opacity → 0 as the body crosses the layer-3 mask, `cubic-bezier(.55,0,.85,.3)`. Waterline emits a ripple ring (scale 1→1.4, opacity 0.5→0, 600ms).
- Hold (900–1500ms): Pebble absent; orb glow lingers (opacity 0.2).
- Resurface (1500–2300ms): rises to rest, opacity 0→1, gentle overshoot `cubic-bezier(.34,1.4,.64,1)`, then resumes idle bob. Total ≈ 2.3s.

**6.3d Fallbacks (locked):**
- `prefers-reduced-motion: reduce` → **all** motion off; render the static surfaced pose; dive replaced by nothing (no crossfade flashing). Hard rule.
- **Touch / coarse pointer** → idle bob stays (amplitude reduced to **4px**); cursor parallax off; **no tap-to-dive.** Pebble stays fully decorative/`aria-hidden`. (This resolves the a11y conflict: an `aria-hidden` element must not be an interactive control. If a tap-to-dive interaction is ever wanted, it must instead be exposed as a labeled `<button>` with a visible focus state, not both.)
- **Save-Data / low-power** → static pose only; auto-dive (trigger b) also disabled. Motion never hijacks scroll position.
- No gyroscope/device-tilt parallax (battery + motion-sickness).

### 6.4 UI micro-motion & icon style

- **Motion tokens (product chrome):** `--motion-fast 120ms` (hover, small state), `--motion-base 200ms` (most transitions), `--motion-slow 320ms` (panels, larger reveals). Standard easing `cubic-bezier(.4,0,.2,1)`, echoing the hero's calm. All UI motion honors `prefers-reduced-motion`.
- **Icon style:** single-stroke line icons on a **24px grid**, stroke **1.75px** (≈ the mark's keyline proportion), **rounded caps and joins**, corner radius ≥ 2px. One color per icon: deepwater on light, mist on dark; interactive icons may take `current`. No duotone, no filled+stroked mixes. The graph UI's type/confidence/language legend glyphs follow this style.

### 6.5 The live graph aesthetic (owns cliché-(c) defense)

The product *is* a node-graph, so the graph screen is the highest "floating-node crypto-network" risk and must be governed: the live graph uses the **lake palette**, an **organic namespace/mindmap layout**, and the existing **confidence / glyph / language legend** so it reads as *"a lake mapped in depth,"* not a crypto network. The floating-node look is permitted **only in the functional product graph, never in marketing illustration.**

---

## 7. Application

### 7.1 Surface rules

Pebble is painterly on every surface (§5.8); the table below is asset format/placement, not a register
choice. The 16–32px favicon simplification (§2.3) is the only surface using the simplified silhouette.

| Surface | Asset(s) | Format | Background | Rules |
|---|---|---|---|---|
| **Landing hero** | Layered scene (§6.2) + motion (§6.3) | WebP/AVIF + PNG fallback, layers separate | painterly lake, dark-first | Gaze → CTA; ≥8% clear margin; static pose is the no-JS/reduced-motion baseline |
| **Landing sections** | mark or a supporting painterly spot, ≤1 per page | PNG/WebP | mist `#EAF4F4` or deepwater | Never stack two painterly scenes on one page |
| **Docs** | mark in header; optional single banner ≥320px tall per top-level section | PNG/WebP | mist light, deepwater code blocks | — |
| **Graph / tool UI (in-product nav)** | a small bundled glyph (currently a hand-drawn inline SVG, `kb/dashboard/static/dashboard.html`) — painterly raster doesn't inline cheaply at nav-icon size | inline SVG or bundled data-URI, zero external fetch | deepwater dark-first | Ships offline: never load remote assets at runtime. Carries the §7.2 second brand signal |
| **README, GitHub** | hero banner (1280×640 safe), shields badges | PNG/WebP via raw URL; SVG badges | reads on GitHub light **and** dark | Use `<picture>` with light/dark sources; never transparent-on-white only |
| **README, PyPI** | same banner, **absolute URLs only** | PNG (PyPI strips some SVG; no relative paths) | PyPI light | All `src` absolute `https://`; one banner, no animation |
| **OG / social** | 1200×630 card | JPG/PNG, exact 1200×630 | painterly, text-safe | Wordmark in real Space Grotesk; title in central 80% safe area |
| **Favicon** | mark (§2.1) ≥48px; simplified silhouette (§2.3) at 16–32px | theme-aware SVG + ICO/PNG @16/32/48 | solid deepwater (never transparent) | never a legible graph at this size |
| **App icon (PWA/touch)** | full painterly mark | PNG @180/192/512 + maskable 512 | deepwater `#0E2A33` solid | Maskable variant with 10% safe padding (Android) |
| **ghcr.io / PyPI avatar** | mark | PNG 512×512 | deepwater solid | Square, no wordmark (registries crop to circle) |

### 7.2 Cross-surface invariants

- **Distinctiveness guard (defeats cliché-(b)):** product chrome must **always carry a second persistent brand signal** so the most-seen screen never reduces to "near-black field + one green accent." Use **lake-blue `#137A8B` structure** alongside the teal, the **gold freshness/active dot**, and the **small mark always present in nav**. Reinforce "**never neon**" so `#2BB3A3` reads teal, not acid.
- The wordmark is always real Space Grotesk, one word, one color, never AI-generated.
- The product UI and any shipped artifact fetch **no external runtime assets**, bundle inline SVG or a data-URI.
- **`shore #D7C5A0`** has exactly one job: a painterly warm-neutral (sand/light, illustration dividers). Never a UI color; never a page background (guards cliché-(a)).
- **Spacing / grid / elevation scale:** out of brand-layer scope; define a token set (e.g. 4px base, 4/8/12/16/24/32/48/64) at the build phase for `BRANDING.md` → site.

### 7.3 Asset checklist

| Asset | Current state | Action | Size / format |
|---|---|---|---|
| **The mark** | **Shipped**: painterly crop, `docs/branding/mark.png` (resize of `docs/img/icon-512.png`) | keep in sync with the painterly source; see §2.1 | PNG |
| `docs/branding/mark.svg`, `mark-dark.svg`, `glyph.svg`, `glyph-mark.svg` | retired flat-mark assets | **Kept for history only** — not referenced by any build, page, or spec; do not revive as the logo | n/a |
| `docs/branding/wordmark.svg` | shipped single-color, weight 700 | keep in sync with §2.4; `<desc>` should not claim a flat otter-head mark is primary | SVG |
| `docs/branding/mascot.md` | rewritten to Section 5, then re-corrected 2026-07-28 (brown fur, one register) | keep in sync with §5 | markdown |
| `docs/img/icon-16/32/48/64.png` | simplified silhouette (`site/tools/gen_small_mark.py`) | **Recolor** the silhouette off mist-teal toward the brand's warm-brown direction so the small-favicon tier doesn't read as a leftover cool otter; not yet done | PNG, deepwater-solid bg |
| `docs/img/icon-180/192/512.png` | painterly mark (`site/tools/gen_icons_final.py`) | **Keep** — already the target state | PNG, deepwater-solid bg |
| `favicon.svg` | missing | **Create** theme-aware flat favicon (`prefers-color-scheme`) | SVG + ICO/PNG @16/32/48 |
| Maskable app icon | missing | **Create** full-bleed deepwater, 10% safe padding | PNG 512×512 |
| `docs/img/og.jpg` (1200×630) | correct spec | **Keep**; verify wordmark is real Space Grotesk overlay | JPG/PNG 1200×630 |
| README hero banner | missing | **Create** RICH, light/dark-safe, absolute-URL hosted | 1280×640, PNG + WebP |
| `<picture>` light/dark banner pair | missing | **Create** for GitHub theme-switching | PNG/WebP |
| `docs/img/architecture.jpg`, `graph.jpg` | product screenshots | **Keep** as-is (not brand illustration) | JPG |
| `/brand` page assets | missing | **Export** dual-register comparison, palette swatches, motion stills | per surface |

**Format law:** vector (SVG) for everything flat; raster (WebP/AVIF primary, PNG fallback) for everything painterly; JPG only where a platform mandates it. Site assets may use modern formats; **product-shipped** assets are inline SVG, bundled, zero runtime fetch.

---

## Handles & namespaces

Prefer the bare name **`contextlake`** everywhere it's free (PyPI is confirmed open; do the
GitHub/domain/social sweep before relying on the rest). Where it's already taken, fall back
to **one** consistent house handle rather than a different variant per site:

- **House handle:** **`contextlakehq`**, for X/Twitter and any platform where the bare name
  is unavailable. Use it consistently so the brand is predictable to find.
- **Reddit:** the brand lives in the subreddit **`r/contextlake`** (a separate namespace
  from usernames, so it stays free even when `u/contextlake` is taken).
- **Playful channels** (Discord, Mastodon/Fosstodon, Bluesky): Pebble's name is an on-brand
  alternative.

**Where to spend effort** (this is a developer tool): GitHub org · PyPI · npm · Docker Hub ·
Hugging Face · Discord · Mastodon/Fosstodon · Bluesky · Dev.to · X/Twitter · Reddit. Skip
commerce, lifestyle, and defunct platforms; they do nothing for an open-source tool.

## Future scope

When contextlake reaches beyond GitLab (GitHub, Bitbucket, DockerHub, Hugging Face...),
**nothing in this guide needs to change**. Each new source is just another stream feeding the
same lake: the name, palette, voice, and mascot all still fit. That future-proofing is the
whole reason for the name.
