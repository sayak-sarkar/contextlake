# contextlake mascot

> Canon for Pebble and the context-pebble. The full system lives in
> [BRANDING.md](../../BRANDING.md) section 5; this file is the working spec for generating
> artwork. Finished art needs a designer or an image model. No em-dashes; lowercase
> "contextlake" always.

## Who Pebble is

**Pebble** is a small, friendly, innocent-faced **dark blue-grey river otter** who surfaces
from the contextlake cradling a glowing translucent **context-pebble** in both forepaws,
offering it forward. Read in one beat: "I went down, I found the real thing, here it is."
Calm, competent, never frantic. Pebble embodies the anti-hallucination promise: it returns
with real context, not a guess.

## Silhouette and proportions

- Rounded, buoyant, river-smoothed. Soft pebble-like mass, no sharp anatomy.
- Head-to-body ratio 1:1.6 (deliberately juvenile and cute).
- Broad rounded head; short muzzle (35% of head width or less); small rounded ears set wide and low.
- Large round dark eyes about 40% down the face, one soft upper-left catch-light only.
- Small rounded heart/triangle nose in deepwater.
- Small 4-digit forepaws always cradling the pebble from below (cup, never grip).
- Recognizable as a solid silhouette at 10% size, with the pebble glow as the single brightest point.

## Fur and body color (illustration ramp)

| Zone | Hex |
| --- | --- |
| Primary fur (back, head, tail) | `#23424B` |
| Mid fur / form transition (also the flat mark body) | `#2F5A63` |
| Belly, chest, muzzle, brow | `#6E8E92` |
| Inner-ear / paw-pad | `#8A9C8F` |
| Nose, eye iris | `#0E2A33` (deepwater, the darkest allowed point) |
| Eye catch-light | `#EAF4F4` |

Wet-fur sheen is soft mist highlights at low opacity, never discrete water droplets.

## The context-pebble (the most controlled element)

- Smooth polished **translucent agate / sea-glass**, lit from within; a rounded organic
  pebble, slightly taller than wide. No facet edges that read as a cut gem.
- Inner glow: core **current `#2BB3A3`** falling off to **lake `#137A8B`**, with a thin
  **mist `#EAF4F4`** rim where it meets dark fur. The glow is the brightest value present.
- Exactly **one** small warm **sun `#E7B53C`** glint near the upper third. One glint, never a constellation.
- It casts a soft teal glow onto Pebble's paws, chest, and chin.
- Painterly scale: 45 to 55% of head width (a co-subject, never larger than the head).

**Hard rule:** NOT an amber sphere with star-dots (Dragon Ball), NOT a faceted crystal or
gem, NOT a gold nugget, NOT a node-graph inside. The knowledge-graph idea lives in large
illustration and the product UI, never inside the carried pebble.

## Expressions (the only approved set)

Calm-warm (default), quietly proud / presenting (at a CTA), curious-focused (looking toward
content), friendly-delight (sparing, for success). Out of canon: anxious, sweating,
surprised, angry, sad, sleepy, winking, tongue-out, any overacting.

## Poses

Presenting (hero, front-facing at the waterline, both paws forward), diving / snorkel
(fetching or indexing states, may be empty-pawed), surfacing (loading to success), peeking
corner accent (small, decorative), resting-on-pebble (idle, glow dimmed). **Gaze always
points toward the content, the CTA, or the pebble, never off-page.**

## Dual register

- **Painterly Pebble:** rich, dimensional, subsurface glow, soft volumetric light, no hard
  outlines (forms separate by value and rim light). For hero, OG/social, large illustration, onboarding.
- **Flat Pebble / logo mark:** bold front-facing otter-head, clean sticker/stamp fills,
  deepwater `#0E2A33` keyline (never black). For logo, favicon, app icon, small UI. See BRANDING.md section 2.

## Do / don't

- **Do:** keep the pebble glow the single brightest point; cast its teal glow onto Pebble;
  aim the painterly gaze at content/CTA/pebble; use deepwater `#0E2A33` as the darkest value.
- **Don't:** node-graph or star-dots inside the pebble; amber sphere, faceted gem, or
  gold-nugget pebble; water droplets on the face; hard black outlines or any `#000`;
  off-page stare; more than one gold glint; anxious/silly expressions; two-Pebble
  compositions; Pebble over a live graph; AI-generated wordmark.

## Image-generation prompt

Generate the painterly background without text, then overlay the wordmark in real Space
Grotesk. Never ship a single AI generation with baked-in lettering. Vary only [POSE] and [CONTEXT].

> Mascot illustration of **Pebble**, a cute innocent-faced **dark blue-grey river otter**
> with a rounded river-smoothed body, broad head (head-to-body ratio 1:1.6), short muzzle,
> large soft dark eyes with a single upper-left catch-light, small rounded ears. Fur in deep
> blue-grey teal tones (`#23424B` back, `#2F5A63` midtone, `#6E8E92` mist-lifted belly),
> deepwater `#0E2A33` nose, no pure black. Pebble is **[POSE]**, cradling in both forepaws a
> **smooth polished translucent agate sea-glass context-pebble with a cool teal-green inner
> glow (`#2BB3A3` core falling off to lake `#137A8B`) and exactly one small warm-gold glint
> (`#E7B53C`)**; the pebble is lit from within and casts a soft teal glow onto Pebble's paws
> and chin. Setting: a calm misty lake at the waterline, **[CONTEXT]**. Calm, trustworthy,
> friendly-but-precise mood. **[STYLE BLOCK]**. Gaze directed toward the context-pebble.
> Palette: deepwater `#0E2A33`, lake `#137A8B`, current `#2BB3A3`, mist `#EAF4F4`, shore
> `#D7C5A0`, gold `#E7B53C`.

**[STYLE BLOCK] painterly:** Rich dimensional painterly illustration, soft volumetric
subsurface lighting, gentle rim light in mist `#EAF4F4`, no hard outlines, forms separated by
value and color, soft depth-of-field lake background.

**[STYLE BLOCK] flat/logo:** Flat bold vector mascot mark, clean filled shapes, front-facing
otter head, single deepwater `#0E2A33` keyline outline (never black), sticker/stamp style,
legible at small sizes, flat background.

**Negative prompt (always include):** amber sphere, glowing orb with star dots, dragon ball,
faceted crystal, cut gem, gold nugget, network graph inside the stone, glowing nodes inside,
water droplets on face, sweat drops, tears, anxious expression, shocked face, angry, sleepy,
hard black outlines, pure black, neon, text, watermark, two otters, realistic otter anatomy,
off-page stare.

**Slot examples:** Hero = [POSE] surfacing at the waterline presenting the pebble forward,
[CONTEXT] soft sunrise mist on calm reflective water, painterly. Loading = [POSE] diving
downward wearing a small snorkel, [CONTEXT] deeper teal underwater light, painterly. Favicon
= [POSE] front-facing head-and-shoulders only, pebble centered below the chin, flat.
