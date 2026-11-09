#!/usr/bin/env python3
"""The contextlake logo, its mark and the favicon set, all from one source file.

SOURCE. `docs/branding/logo-source.png` is the logo exactly as Sayak supplied it on
2026-10-27: 2172x724, an off-white field, no alpha. A line-art otter in the lake holds
a node-graph pebble, beside the two-tone wordmark. Nothing here redraws it. Every
output is that art matted, cut, resized, padded or set on a background.

WHAT IT WRITES

    docs/branding/logo.webp              the logo for light surfaces
    docs/branding/logo-dark.webp         the logo for dark surfaces
    docs/branding/pebble-mark-dark.webp  the otter alone, for dark surfaces (footer,
                                         architecture card)
    docs/branding/pebble-logo.webp       the logo without its wordmark: the otter, its
    docs/branding/pebble-logo.png        pebble and the ripples, the otter's body white,
                                         the background transparent (WebP for the web,
                                         PNG at the source's full resolution)
    docs/img/favicon.ico                 16, 32 and 48 in one file
    docs/img/favicon-16x16.png, favicon-32x32.png
    docs/img/apple-touch-icon.png        180, full-bleed (iOS rounds it itself)
    docs/img/android-chrome-192x192.png, android-chrome-512x512.png   rounded tile
    docs/img/android-chrome-maskable-512x512.png   full-bleed, mark inside the safe circle

MATTING. The otter is line art whose fill is the same white as the field, so "make
white transparent" would hollow it out while the letter counters must still go clear.

    light  colour-to-alpha everywhere: every pixel is un-mixed from the field colour.
           On a light page the field and the otter's fill both become page, exactly
           as drawn.
    dark   the otter keeps its own white fill as a sticker, because its brown line
           work is about 1.3:1 against a dark surface. Everything else is un-mixed to
           alpha, and only the dark-blue "context" turns mist #EAF4F4 so it reads.

This is the method that made the light/dark pair Sayak approved on 2026-10-27; run on
the same source it reproduces that pair to within half a level.

THE MARK is the otter cut out of the logo by colour and position: the teal ripple
strokes below the waterline and the wordmark are removed, and nothing is added.

THE LOGO-ONLY VARIANT (Pebble without the wordmark, Sayak 2026-11-09: "the bg of the
logo should be transparent", "the otter's body can be white") is cut from the dark
matte, so the otter keeps its white fill and reads on light and dark surfaces alike,
while the field around it is transparent. It keeps everything left of the mark's cut
plus the ripple strokes, which run on under the "c", and drops the few faint specks of
field grain left at the edges. The wordmark is the only thing removed; the otter, the
pebble and the water are the logo's own pixels.

THE TILE. Every icon is the dark mark on solid deepwater #0E2A33, never transparent:
a transparent icon vanishes on a dark tab strip (BRANDING.md 2.1, 2.3). The white-filled
otter is the highest-contrast thing the art offers on deepwater. From 180px up the mark
keeps clear space of 20% of its own height (BRANDING.md 2.6). At 16-48px that clear
space would shrink the otter to a speck, so the mark is fitted to the tile with a thin
margin instead; its whiskers go first at that size, and the head, eyes and pebble hold.
Each icon is composed at 8x and reduced once.

Needs numpy, scipy and Pillow with WebP. Run: python3 site/tools/gen_logo_assets.py
Writes into the repo's docs/ (override with CONTEXTLAKE_ASSET_OUT, which gets the same
docs/-style branding/ and img/ subfolders). site/ copies are synced by site/build_docs.py.
"""
import os
import pathlib

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "docs/branding/logo-source.png"
OUT = pathlib.Path(os.environ.get("CONTEXTLAKE_ASSET_OUT", str(ROOT / "docs")))
(OUT / "branding").mkdir(parents=True, exist_ok=True)
(OUT / "img").mkdir(parents=True, exist_ok=True)

DEEP = (14, 42, 51, 255)                       # deepwater #0E2A33
MIST = np.array([0xEA, 0xF4, 0xF4], np.float32)
LOGO_W = 480                                   # header logo: 40px tall shown, ~3x density
MARK_H = 128                                   # footer / card mark: about 30px shown, ~4x


# --- matting (source pixel coordinates) -------------------------------------------
im = np.asarray(Image.open(SRC).convert("RGB")).astype(np.float32)
H, W, _ = im.shape
corners = [im[:24, :24], im[:24, -24:], im[-24:, :24], im[-24:, -24:]]
field = np.median(np.concatenate([c.reshape(-1, 3) for c in corners]), axis=0)


def unmix(rgb):
    # Ink is only ever darker than the field. Brighter pixels are field grain, and
    # treating them as ink turns every 255 into an opaque white speck when the field
    # sits at 254. Grain measures +/-1 level, so a soft floor of 0.02 (about 5 levels)
    # clears it without hard-cutting anti-aliased edges.
    a = np.clip((field - rgb) / np.maximum(field, 1), 0, 1).max(axis=2)
    a = np.clip((a - 0.02) / 0.98, 0, 1)
    safe = np.maximum(a, 1e-6)[..., None]
    fg = np.clip(field + (np.minimum(rgb, field) - field) / safe, 0, 255)
    return fg, a


fg, alpha = unmix(im)

# The otter's interior: white regions not reachable from the image border through
# non-ink pixels. Its body has no closed bottom edge (the outline ends in broken
# ripple strokes), so below the waterline the barrier is dilated to seal those gaps,
# then the interior grows back geodesically, never across ink, to meet the strokes.
ink = np.abs(im - field).max(axis=2) > 60
WORD_X = 640                    # the wordmark starts right of this, the otter's fill left
WATER_Y = int(H * 0.60)         # below this the body meets the broken ripples
R = 9
yy, xx = np.ogrid[-R:R + 1, -R:R + 1]
barrier = ink.copy()
sealed = ndi.binary_dilation(ink, structure=(xx * xx + yy * yy) <= R * R)
barrier[WATER_Y:, :WORD_X] |= sealed[WATER_Y:, :WORD_X]
lab, n = ndi.label(~barrier)
border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
cnt = np.bincount(lab.ravel(), minlength=n + 1)
cx = np.bincount(lab.ravel(), weights=np.broadcast_to(np.arange(W), (H, W)).ravel(),
                 minlength=n + 1) / np.maximum(cnt, 1)
otter = [lb for lb in range(1, n + 1) if lb not in border and cx[lb] < WORD_X]
# Keep the main fill and only what nests inside its silhouette (pebble glass, paws,
# eye highlights). Pockets between ripple strokes sit outside it: lake, not otter.
main = max(otter, key=lambda lb: cnt[lb])
objs = ndi.find_objects(lab)
mx0, mx1, my1 = objs[main - 1][1].start, objs[main - 1][1].stop, objs[main - 1][0].stop


def nested(lb):
    sl = objs[lb - 1]
    return (sl[1].start >= mx0 - 2 and sl[1].stop <= mx1 + 2
            and (sl[0].start + sl[0].stop) / 2 <= my1 + 15)


interior = np.isin(lab, [lb for lb in otter if lb == main or nested(lb)])
for _ in range(R + 1):
    interior = ndi.binary_dilation(interior) & ~ink

# light: the pure un-mix
light = np.dstack([fg, alpha * 255])

# dark: everything inside the otter's silhouette (its fill plus the line work and
# pebble it encloses) is opaque in its original colour; the fill's outer edge is
# softened where it has no outline to bound it (below the paws).
zone = np.zeros((H, W), bool)
zone[:, :WORD_X] = True
sticker = ndi.binary_fill_holes(interior | (ink & zone)) & zone
soft = ndi.gaussian_filter(ndi.binary_fill_holes(interior).astype(np.float32), sigma=1.6)
a_fill = np.clip(soft / 0.5, 0, 1)
fg_d, a_d = fg.copy(), alpha.copy()
fg_d[sticker] = im[sticker]
a_d[sticker] = 1.0
edge = (~sticker) & (a_fill > a_d)
fg_d[edge] = im[edge]
a_d[edge] = a_fill[edge]
word = ~zone
is_context = word & (a_d > 0) & (np.abs(fg_d - np.array([3, 48, 74], np.float32)).max(axis=2) < 48)
fg_d[is_context] = MIST
dark = np.dstack([fg_d, a_d * 255])


def to_img(arr):
    return Image.fromarray(arr.round().clip(0, 255).astype(np.uint8), "RGBA")


bbox = to_img(light).getchannel("A").getbbox()
logo_l = to_img(light).crop(bbox)
logo_d = to_img(dark).crop(bbox)


def save_webp(img, width, path):
    h = round(img.height * width / img.width)
    img.resize((width, h), Image.LANCZOS).save(path, "WEBP", quality=92, method=6,
                                                alpha_quality=100)
    print(f"  branding/{path.name}  {width}x{h}")


save_webp(logo_l, LOGO_W, OUT / "branding/logo.webp")
save_webp(logo_d, LOGO_W, OUT / "branding/logo-dark.webp")


# --- the mark: the otter cut out of the logo (crop coordinates) --------------------
L = np.asarray(logo_l).astype(np.float32)
D = np.asarray(logo_d).astype(np.float32)
h, w = L.shape[:2]
ZX = round(w * 430 / 1200)      # the otter and its ripples sit left of this
WY = round(h * 205 / 316)       # ripple strokes start below this
r, g, b, a = (L[..., i] for i in range(4))
teal = (a > 20) & (g > r + 40) & (b > r + 40)
teal[:, ZX:] = False
tl, tn = ndi.label(teal, structure=np.ones((3, 3)))
tobj = ndi.find_objects(tl)
ripple_ids = [i + 1 for i, s in enumerate(tobj) if s is not None and s[0].start >= WY]
keep_teal = np.isin(tl, [i for i in range(1, tn + 1) if i not in ripple_ids])
ripple = (ndi.binary_dilation(np.isin(tl, ripple_ids), iterations=3)
          & ~ndi.binary_dilation(keep_teal, iterations=1))
# With the ripples gone, a run of empty columns separates the whiskers from the "c".
occupied = ((a > 8) & ~ripple).any(axis=0)
gap = [x for x in range(int(w * 0.25), ZX) if not occupied[x]]
if not gap:
    raise SystemExit("gen_logo_assets: no clear column between the otter and the wordmark")
cut_x = (gap[0] + gap[-1]) // 2


def cut(arr):
    m = arr[:, :cut_x].copy()
    m[..., 3][ripple[:, :cut_x]] = 0
    img = to_img(m)
    return img.crop(img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox())


mark_d = cut(D)
mw = round(mark_d.width * MARK_H / mark_d.height)
mark_d.resize((mw, MARK_H), Image.LANCZOS).save(
    OUT / "branding/pebble-mark-dark.webp", "WEBP", quality=92, method=6, alpha_quality=100)
print(f"  branding/pebble-mark-dark.webp  {mw}x{MARK_H}  (mark cut at x={cut_x} of {w})")


# --- the logo without its wordmark: the otter left of the cut, plus its ripples ------
PEBBLE_LOGO_H = 256                            # web copy; the PNG keeps the source resolution
keep = np.zeros((h, w), bool)
keep[:, :cut_x] = True
keep |= ripple


def logo_only(arr):
    m = arr.copy()
    m[..., 3][~keep] = 0
    solid = m[..., 3] > 8
    lab_, n_ = ndi.label(solid, structure=np.ones((3, 3)))
    size = np.bincount(lab_.ravel(), minlength=n_ + 1)
    big = np.isin(lab_, [i for i in range(1, n_ + 1) if size[i] >= 25])   # specks of grain go
    m[..., 3][~ndi.binary_dilation(big, iterations=2)] = 0
    img = to_img(m)
    return img.crop(img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox())


pebble_logo = logo_only(D)
pebble_logo.save(OUT / "branding/pebble-logo.png", optimize=True)
pw = round(pebble_logo.width * PEBBLE_LOGO_H / pebble_logo.height)
pebble_logo.resize((pw, PEBBLE_LOGO_H), Image.LANCZOS).save(
    OUT / "branding/pebble-logo.webp", "WEBP", quality=92, method=6, alpha_quality=100)
print(f"  branding/pebble-logo.png  {pebble_logo.width}x{pebble_logo.height},  "
      f"pebble-logo.webp  {pw}x{PEBBLE_LOGO_H}")


# --- icons --------------------------------------------------------------------------
def tile(size, mode, rounded):
    """mode 'small': fit the mark with a thin margin; 'clear': 20% clear space;
    'maskable': the mark's box inside the central 80% safe circle."""
    S = size * 8
    c = Image.new("RGBA", (S, S), DEEP)
    mw_, mh_ = mark_d.size
    if mode == "small":
        k = S * 0.92 / max(mw_, mh_)
    elif mode == "clear":
        k = S / max(mw_ + 0.4 * mh_, 1.4 * mh_)
    else:
        k = S * 0.80 / (mw_ ** 2 + mh_ ** 2) ** 0.5
    m = mark_d.resize((round(mw_ * k), round(mh_ * k)), Image.LANCZOS)
    c.alpha_composite(m, ((S - m.width) // 2, (S - m.height) // 2))
    if not rounded:             # full-bleed: opaque, no alpha channel at all
        return c.convert("RGB").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22),
                                           fill=255)
    c.putalpha(mask)
    return c.resize((size, size), Image.LANCZOS)


small = {s: tile(s, "small", True) for s in (16, 32, 48)}
small[16].save(OUT / "img/favicon-16x16.png", optimize=True)
small[32].save(OUT / "img/favicon-32x32.png", optimize=True)
small[48].save(OUT / "img/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)],
               append_images=[small[16], small[32]])
tile(180, "clear", False).save(OUT / "img/apple-touch-icon.png", optimize=True)
tile(192, "clear", True).save(OUT / "img/android-chrome-192x192.png", optimize=True)
tile(512, "clear", True).save(OUT / "img/android-chrome-512x512.png", optimize=True)
tile(512, "maskable", False).save(OUT / "img/android-chrome-maskable-512x512.png",
                                  optimize=True)
print(f"  favicon set written to {OUT / 'img'}")
