#!/usr/bin/env python3
"""Draws extension/icon.png: a sound wave between code braces. Needs Pillow."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE, SCALE = 256, 4
S = SIZE * SCALE
BG, BRACE, WAVE = (27, 31, 42), (125, 138, 168), (94, 234, 212)

image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, S - 1, S - 1), radius=S * 0.2, fill=BG)

font = None
for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                  "/System/Library/Fonts/Menlo.ttc"):
    if Path(candidate).exists():
        font = ImageFont.truetype(candidate, int(S * 0.62))
        break
if font is None:
    raise SystemExit("no monospace font found")
for glyph, x in (("{", S * 0.17), ("}", S * 0.83)):
    draw.text((x, S * 0.47), glyph, font=font, fill=BRACE, anchor="mm")

# Five bars of a voice waveform, centred.
heights = [0.16, 0.34, 0.52, 0.30, 0.14]
width, gap = S * 0.052, S * 0.036
total = len(heights) * width + (len(heights) - 1) * gap
x = (S - total) / 2
for h in heights:
    half = S * h / 2
    draw.rounded_rectangle((x, S / 2 - half, x + width, S / 2 + half), radius=width / 2, fill=WAVE)
    x += width + gap

out = Path(__file__).resolve().parent.parent / "extension" / "icon.png"
image.resize((SIZE, SIZE), Image.LANCZOS).save(out)
print(out)
