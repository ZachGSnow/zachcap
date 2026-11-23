"""Draw the ZachCap app icon: the sidebar's waveform logo (Lucide "audio-lines"),
white on a black rounded square, as a 1024 px PNG.

    uv run --project ../../engine python scripts/make-icon.py app-icon.png
    npx tauri icon app-icon.png
"""

import sys

from PIL import Image, ImageDraw

SIZE = 1024
BACKGROUND = (10, 10, 10, 255)
FOREGROUND = (250, 250, 250, 255)
# Lucide audio-lines on its 24-unit grid: (x, y1, y2) per stroke, stroke width 2, round caps.
STROKES = [(2, 10, 13), (6, 6, 17), (10, 3, 21), (14, 8, 15), (18, 5, 18), (22, 10, 13)]
UNIT = 25.0  # pixels per grid unit: the glyph is about 54% of the icon


def px(units: float) -> float:
    return SIZE / 2 + (units - 12) * UNIT


image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=225, fill=BACKGROUND)
half = UNIT  # half of the 2-unit stroke
for x, y1, y2 in STROKES:
    draw.rounded_rectangle(
        (px(x) - half, px(y1) - half, px(x) + half, px(y2) + half), radius=half, fill=FOREGROUND
    )

image.save(sys.argv[1] if len(sys.argv) > 1 else "app-icon.png")
