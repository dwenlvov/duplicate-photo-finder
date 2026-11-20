"""Draws the Mac app icon: the same two photos as the menu-bar icon and the browser-tab icon, on a rounded tile.

Usage (from the project root, on a Mac): .venv/bin/python packaging/make_icon.py
Writes packaging/icon.icns, which packaging/mac.spec puts into the app.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icon.icns"
BLUE = (45, 99, 163, 255)       # the page's accent (--similar)
TILE = (247, 249, 251, 255)
EDGE = (200, 208, 216, 255)     # the page's --rule
PHOTO = (255, 255, 255, 255)
SKY = (220, 231, 243, 255)


def draw(n: int = 1024) -> Image.Image:
    s = 4  # draw large, then scale down: smooth edges
    N = n * s
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    u = N / 1024
    # macOS icon grid: the tile takes 824 of 1024 px, corners about 185 px
    d.rounded_rectangle([100 * u, 100 * u, 924 * u, 924 * u], radius=185 * u, fill=TILE, outline=EDGE, width=int(6 * u))
    w = int(34 * u)
    # back photo
    d.rounded_rectangle([230 * u, 250 * u, 640 * u, 590 * u], radius=36 * u, fill=PHOTO, outline=BLUE, width=w)
    # front photo, over the back one
    x0, y0, x1, y1 = 380 * u, 420 * u, 800 * u, 780 * u
    d.rounded_rectangle([x0, y0, x1, y1], radius=36 * u, fill=SKY, outline=BLUE, width=w)
    # a hill and the sun inside the front photo
    d.polygon([(x0 + 50 * u, y1 - 50 * u), (x0 + 160 * u, y0 + 190 * u), (x0 + 250 * u, y0 + 270 * u),
               (x0 + 300 * u, y0 + 220 * u), (x1 - 50 * u, y1 - 50 * u)], fill=BLUE)
    r = 34 * u
    cx, cy = x1 - 110 * u, y0 + 105 * u
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLUE)
    return im.resize((n, n), Image.LANCZOS)


def main():
    big = draw(1024)
    tmp = Path(tempfile.mkdtemp())
    iconset = tmp / "icon.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        big.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        big.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
    big.save(tmp / "preview.png")
    shutil.copy(tmp / "preview.png", OUT.with_suffix(".png"))
    print(OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
