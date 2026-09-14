#!/usr/bin/env python3
"""Generate the Seplos HV BMS integration icon with Pillow.

Concept: a rounded-square tile, deep navy/charcoal gradient background, a
stylised HV battery pack in the centre drawn as a stack of four horizontal
cell modules (the 4 BMU modules) in teal/green with a lighter top module, a
bold amber lightning-bolt "HV" energy accent overlaid, and a thin cyan
telemetry sparkline running across the lower third. Max 3 accent colours
(teal, amber, cyan). No SVG rasterizer required -- pure Pillow.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 2048  # master (supersampled) size
R_TILE = int(S * 0.18)  # tile corner radius


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_col(c1, c2, t):
    return tuple(int(round(lerp(c1[i], c2[i], t))) for i in range(len(c1)))


def clip(layer, mask_l):
    r, g, b, a = layer.split()
    a = ImageChops.multiply(a, mask_l)
    return Image.merge("RGBA", (r, g, b, a))


def rrect_mask(size, box, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


img = Image.new("RGBA", (S, S), (0, 0, 0, 255))
d = ImageDraw.Draw(img)

# ---- 1. Background: deep navy -> charcoal diagonal gradient ----------------
navy_tl = (16, 26, 46, 255)
char_br = (30, 33, 39, 255)
for y in range(S):
    ty = y / (S - 1)
    row_top = lerp_col(navy_tl, char_br, ty * 0.55)
    row_bot = lerp_col(navy_tl, char_br, 0.55 + ty * 0.45)
    d.line([(0, y), (S, y)], fill=lerp_col(row_top, row_bot, 0.5))

# subtle vignette for depth
vig = Image.new("L", (S, S), 0)
vd = ImageDraw.Draw(vig)
vd.ellipse([-S * 0.25, -S * 0.25, S * 1.25, S * 1.25], fill=70)
vig = vig.filter(ImageFilter.GaussianBlur(S * 0.14))
dark = Image.new("RGBA", (S, S), (0, 0, 0, 90))
img.alpha_composite(Image.merge("RGBA", (*dark.split()[:3], vig.point(lambda v: 90 - v if v < 90 else 0))))

# soft teal glow behind the pack (focus/energy halo)
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse(
    [S * 0.18, S * 0.14, S * 0.82, S * 0.86], fill=(60, 170, 150, 55)
)
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.09)))

# ---- 2. Battery pack: stack of 4 horizontal modules -------------------------
pack_l, pack_r = S * 0.305, S * 0.695
pack_t, pack_b = S * 0.240, S * 0.700
pack_w = pack_r - pack_l
pack_h = pack_b - pack_t

# drop shadow under the whole pack
sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(sh).rounded_rectangle(
    [pack_l - S * 0.01, pack_t + S * 0.03, pack_r + S * 0.01, pack_b + S * 0.045],
    radius=int(S * 0.03), fill=(0, 0, 0, 130),
)
img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(S * 0.02)))

# terminal nub on top (reads as "battery" at a glance)
nub_w, nub_h = pack_w * 0.30, S * 0.028
d.rounded_rectangle(
    [pack_l + pack_w / 2 - nub_w / 2, pack_t - nub_h, pack_l + pack_w / 2 + nub_w / 2, pack_t + S * 0.006],
    radius=int(S * 0.008), fill=(160, 170, 180, 255),
)

n_mod = 4
gap = S * 0.016
mod_h = (pack_h - gap * (n_mod - 1)) / n_mod
mod_r = int(S * 0.020)

teal_top = (108, 214, 186, 255)
teal_body = (40, 138, 118, 255)
teal_body_dark = (30, 112, 96, 255)

for i in range(n_mod):
    y0 = pack_t + i * (mod_h + gap)
    y1 = y0 + mod_h
    fill = teal_top if i == 0 else lerp_col(teal_body, teal_body_dark, (i - 1) / max(1, n_mod - 2))
    d.rounded_rectangle([pack_l, y0, pack_r, y1], radius=mod_r, fill=fill)
    # thin top highlight sliver inside each module for a flat "soft shadow" bevel
    hi = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hi).rounded_rectangle(
        [pack_l, y0, pack_r, y0 + mod_h * 0.30], radius=mod_r, fill=(255, 255, 255, 26)
    )
    mask = rrect_mask((S, S), [pack_l, y0, pack_r, y1], mod_r)
    img.alpha_composite(clip(hi, mask))
    # bottom shade sliver
    lo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(lo).rounded_rectangle(
        [pack_l, y1 - mod_h * 0.22, pack_r, y1], radius=mod_r, fill=(0, 0, 0, 30)
    )
    img.alpha_composite(clip(lo, mask))

# outer pack outline for crispness
d.rounded_rectangle([pack_l, pack_t, pack_r, pack_b], radius=int(S * 0.026), outline=(12, 16, 22, 160), width=max(2, int(S * 0.006)))

# ---- 3. Lightning-bolt "HV" energy accent overlaid --------------------------
bolt_pts_unit = [
    (0.60, 0.00), (0.22, 0.56), (0.44, 0.56),
    (0.32, 1.00), (0.80, 0.40), (0.54, 0.40),
]
bx0, by0, bx1, by1 = S * 0.365, S * 0.155, S * 0.700, S * 0.845
bw, bh = bx1 - bx0, by1 - by0
bolt = [(bx0 + ux * bw, by0 + uy * bh) for ux, uy in bolt_pts_unit]

# soft dark shadow behind bolt for pop
bolt_shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
off = S * 0.010
ImageDraw.Draw(bolt_shadow).polygon([(x + off, y + off * 1.4) for x, y in bolt], fill=(0, 0, 0, 130))
img.alpha_composite(bolt_shadow.filter(ImageFilter.GaussianBlur(S * 0.012)))

amber_top, amber_bot = (255, 186, 64, 255), (255, 120, 28, 255)
bolt_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bld = ImageDraw.Draw(bolt_layer)
bld.polygon(bolt, fill=amber_top)
# vertical-ish gradient tint using a mask
grad = Image.new("L", (S, S), 0)
gd = ImageDraw.Draw(grad)
for yy in range(int(by0), int(by1) + 1):
    t = (yy - by0) / bh
    gd.line([(0, yy), (S, yy)], fill=int(255 * t))
bmask = Image.new("L", (S, S), 0)
ImageDraw.Draw(bmask).polygon(bolt, fill=255)
tint = Image.new("RGBA", (S, S), amber_bot)
tint.putalpha(ImageChops.multiply(grad, bmask))
img.alpha_composite(clip(bolt_layer, bmask))
img.alpha_composite(tint)
ImageDraw.Draw(img).polygon(bolt, outline=(120, 58, 8, 200), width=max(2, int(S * 0.0045)))

# ---- 4. Cyan telemetry sparkline across the lower third ---------------------
# Kept short, bold and low-frequency (one clear peak + one trough) so the
# zigzag still survives a downscale to 32px -- a busy multi-peak line just
# smears into a grey band at that size.
spark_y = S * 0.820
spark_x0, spark_x1 = S * 0.175, S * 0.825
pts_unit = [
    (0.00, 0.30), (0.16, 0.30), (0.32, -0.95), (0.50, 0.30),
    (0.66, 0.30), (0.80, 0.95), (1.00, 0.10),
]
amp = S * 0.040
spark_pts = [(spark_x0 + ux * (spark_x1 - spark_x0), spark_y + uy * amp) for ux, uy in pts_unit]

cyan = (120, 238, 248, 255)
glow_line = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow_line).line(spark_pts, fill=(120, 238, 248, 190), width=max(6, int(S * 0.020)), joint="curve")
img.alpha_composite(glow_line.filter(ImageFilter.GaussianBlur(S * 0.012)))
d.line(spark_pts, fill=cyan, width=max(4, int(S * 0.010)), joint="curve")
for x, y in spark_pts:
    d.ellipse([x - S * 0.005, y - S * 0.005, x + S * 0.005, y + S * 0.005], fill=cyan)
# live "ping" dot at the last point
px, py = spark_pts[-1]
ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(ring).ellipse([px - S * 0.032, py - S * 0.032, px + S * 0.032, py + S * 0.032], fill=(120, 238, 248, 90))
img.alpha_composite(ring.filter(ImageFilter.GaussianBlur(S * 0.007)))
d.ellipse([px - S * 0.016, py - S * 0.016, px + S * 0.016, py + S * 0.016], fill=cyan)

# ---- 5. Clip everything to the rounded tile + subtle inner stroke ----------
tile = Image.new("L", (S, S), 0)
ImageDraw.Draw(tile).rounded_rectangle([0, 0, S - 1, S - 1], radius=R_TILE, fill=255)
final = Image.new("RGBA", (S, S), (0, 0, 0, 0))
final.paste(img, (0, 0), tile)
ImageDraw.Draw(final).rounded_rectangle(
    [int(S * 0.012)] * 2 + [int(S - S * 0.012)] * 2,
    radius=int(R_TILE * 0.92), outline=(255, 255, 255, 55), width=max(2, int(S * 0.006)),
)

# ---- 6. Export ---------------------------------------------------------------
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = {
    "icons/icon.png": 256,
    "icons/icon@2x.png": 512,
    "icons/preview_64.png": 64,
    "icons/logo.png": 1024,
    "brands/custom_integrations/seplos_hv/icon.png": 256,
    "brands/custom_integrations/seplos_hv/icon@2x.png": 512,
}
for rel, size in TARGETS.items():
    path = f"{BASE}/{rel}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    final.resize((size, size), Image.LANCZOS).save(path)
print("wrote:", ", ".join(TARGETS))
