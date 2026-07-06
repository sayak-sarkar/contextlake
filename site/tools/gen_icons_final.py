#!/usr/bin/env python3
"""Final painterly icon set from the clean-orb master.
Dual register: tight-crop (face + orb) for small favicons that must stay
legible; full-body painterly for the large app icons that can show detail."""
from PIL import Image, ImageDraw, ImageFilter
import os
import pathlib

# The master mascot art lives outside the repo (brand-asset lab); point at it via
# CONTEXTLAKE_MASTER_MARK. STAGE defaults to the repo's docs/img.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
MASTER = os.environ.get("CONTEXTLAKE_MASTER_MARK", str(_ROOT / "docs/img/icon-512.png"))
STAGE = os.environ.get("CONTEXTLAKE_ICON_STAGE", str(_ROOT / "docs/img/icons-out"))
os.makedirs(STAGE, exist_ok=True)
DEEP = (14, 42, 51, 255)   # #0E2A33
LAKE = (19, 122, 139)

full = Image.open(MASTER).convert("RGBA")
full = full.crop(full.getbbox())
fw, fh = full.size
tight = full.crop((0, 0, fw, int(fh * 0.82)))   # face + orb, drops lower body


def rmask(S, r):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=255)
    return m


def water_band(S):
    band = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.ellipse([int(-S * 0.25), int(S * 0.74), int(S * 1.25), int(S * 1.3)],
               fill=(*LAKE, 70))
    return band.filter(ImageFilter.GaussianBlur(S * 0.03))


def render(size, src, frac, dy, rounded=True, band=True, ss=8):
    S = size * ss
    c = Image.new("RGBA", (S, S), DEEP)
    if band:
        c = Image.alpha_composite(c, water_band(S))
    tw = int(S * frac)
    th = int(tw * src.size[1] / src.size[0])
    o = src.resize((tw, th), Image.LANCZOS)
    c.alpha_composite(o, ((S - tw) // 2, int((S - th) / 2 + S * dy)))
    if rounded:
        c.putalpha(rmask(S, int(S * 0.225)))
    return c.resize((size, size), Image.LANCZOS)


# small favicons: tight crop, orb anchors legibility
for s in (16, 32, 48, 64):
    render(s, tight, 1.02, 0.04).save(f"{STAGE}/icon-{s}.png", optimize=True)
# large app icons: full-body painterly
for s in (180, 192, 512):
    render(s, full, 0.80, 0.05).save(f"{STAGE}/icon-{s}.png", optimize=True)
# maskable: full-bleed, safe-area otter, no rounding
render(512, full, 0.60, 0.0, rounded=False).save(f"{STAGE}/icon-maskable-512.png", optimize=True)
# transparent header/footer mark (no deepwater plate) for light + dark surfaces
markS = 256
mk = Image.new("RGBA", (markS, markS), (0, 0, 0, 0))
tw = int(markS * 1.0); th = int(tw * tight.size[1] / tight.size[0])
mk.alpha_composite(tight.resize((tw, th), Image.LANCZOS), ((markS - tw) // 2, (markS - th) // 2))
mk.save(f"{STAGE}/mark.png", optimize=True)
print("icon set written to", STAGE)
