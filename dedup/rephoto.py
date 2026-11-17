"""Was the new photo taken of a screen (or a print) showing the old one?

The new photo is aligned to the old one (ORB + homography). If the whole old frame lands inside the new
photo and takes at most 90% of it, the matching area is cut out, straightened and compared with SSCD again.
A re-shot screen loses most of its SSCD similarity to the bezel, desk and glare (0.46-0.52 on our
simulations); straightened it is back to 0.67-0.91.

Measured on 2026-11-15 (experiments/rephoto_2026-11-15):
- simulated screen photos of 157 Copydays originals: of those that could be aligned (197 of 314),
  all 197 flagged; the other 117 (flat, low-texture pictures) could not be aligned and are not flagged;
- ordinary edits (crop 20/50%, JPEG 20, strong edits): 0 flagged;
- honest photos of the same place (Holidays, 1204 pairs): 28 flagged, mostly shot from further back,
  so the old view fits inside the new frame. A flag is a hint for a person, never a copy verdict.

A straightened match of 0.95 or more means the old photo is simply a cropped part of the new one: a copy,
not a re-shot screen (experiments/rephoto_2026-11-15/results/crop_rule.txt: Copydays crops 1516 of 1528 at
>= 0.95, simulated screens 0 of 299 (max 0.924), the 28 honest same-place pairs above: 2).
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import features, series

REPHOTO_VERSION = "r2-640-inside-area0.9-rect0.6-crop0.95"
SIDE = 640
MIN_INLIERS = 60
MAX_AREA = 0.90       # the old frame takes at most this share of the new photo
MIN_RECT_SSCD = 0.60  # SSCD of the straightened area vs the old photo
CROP_RECT_SSCD = 0.95  # from here on the old photo is a plain crop of the new one


def _load(path: str) -> Image.Image:
    im = Image.open(path)
    im.draft("RGB", (SIDE * 2, SIDE * 2))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((SIDE, SIDE), Image.BILINEAR)
    return im


def check(old_path: str, new_path: str) -> dict:
    """{'rephoto': True/False/None, 'crop': bool, 'rect': float|None, 'area': float|None, 'inliers': int}.
    rephoto None: could not tell. crop True: the old photo is a cropped part of the new one (a copy)."""
    o, n = _load(old_path), _load(new_path)
    og, ng = np.asarray(o.convert("L")), np.asarray(n.convert("L"))
    al = series._align(og, ng)
    if al is None or al[0] < MIN_INLIERS:
        return dict(rephoto=None, crop=False, rect=None, area=None, inliers=al[0] if al else 0)
    inl, H, bb = al
    src = np.asarray(n)
    if not np.array_equal(bb, ng):
        src = np.ascontiguousarray(src[:, ::-1])
    corners = np.float32([[0, 0], [o.width, 0], [o.width, o.height], [0, o.height]]).reshape(-1, 1, 2)
    q = cv2.perspectiveTransform(corners, np.linalg.inv(H)).reshape(-1, 2)
    W, Hh = n.width, n.height
    inside = all(-0.02 * W <= x <= 1.02 * W and -0.02 * Hh <= y <= 1.02 * Hh for x, y in q)
    area = abs(cv2.contourArea(q)) / (W * Hh)
    if not inside or area > MAX_AREA:
        return dict(rephoto=False, crop=False, rect=None, area=round(float(area), 3), inliers=inl)
    w = cv2.warpPerspective(src, H, (o.width, o.height))
    m = cv2.warpPerspective(np.full(src.shape[:2], 255, np.uint8), H, (o.width, o.height)) > 0
    ys, xs = np.where(m)
    if len(ys) < 0.3 * m.size:
        return dict(rephoto=None, crop=False, rect=None, area=round(float(area), 3), inliers=inl)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    a = features._sscd(Image.fromarray(w[y0:y1, x0:x1])).astype(np.float32)
    b = features._sscd(o.crop((int(x0), int(y0), int(x1), int(y1)))).astype(np.float32)
    rect = float(a @ b)
    crop = rect >= CROP_RECT_SSCD
    return dict(rephoto=MIN_RECT_SSCD <= rect < CROP_RECT_SSCD, crop=crop, rect=round(rect, 3), area=round(float(area), 3), inliers=inl)
