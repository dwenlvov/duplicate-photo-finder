"""Two frames of a series, or one photo edited?

A pair that looks like a copy is aligned (ORB + homography), then compared with a few pixels of slack.
An edit (recompression, filter, crop, caption, screenshot frame) leaves the overlapping picture the same;
a second frame of a series has things that moved, so a noticeable part of the frame differs.

Measured on 2026-11-15 (experiments/rephoto_2026-11-15/results/series_check.txt): of 4396 copies (Copydays crops,
JPEG, mirror plus our black-and-white, 90/180/270 rotation, strong filter with vignette, previews) 4 were called a
series, all extreme (80% crop, JPEG quality 3-30); of 53 found "strong" Copydays edits 0. Brightness is normalised
locally, so vignettes and filters do not count as change (with one global normalisation 25 of 157 filtered copies
were called a series). Our burst pair broadstairs0/1 is caught; a nearly still burst (okeedokee0/1, people barely
moved) is not, so the camera-time rule stays the main signal for bursts.
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageOps

SERIES_VERSION = "s2-640-orb2000-localnorm12-env7-grid24x32"
SIDE = 640            # both photos are compared at this long side
MIN_INLIERS = 60      # below this the alignment is not trusted and no verdict is given
MIN_SHARE = 0.14      # share of grid cells that changed: copies stayed below 0.10, burst broadstairs0/1: 0.17
CELL_DIFF = 0.35      # a cell "changed" when its 75th-percentile difference (in std units) is above this

_orb = cv2.ORB_create(2000)
_bf = cv2.BFMatcher(cv2.NORM_HAMMING)


def _gray(path: str) -> np.ndarray:
    im = Image.open(path)
    im.draft("L", (SIDE * 2, SIDE * 2))
    im = ImageOps.exif_transpose(im).convert("L")
    im.thumbnail((SIDE, SIDE), Image.BILINEAR)
    return np.asarray(im)


def _align(a: np.ndarray, b: np.ndarray):
    ka, da = _orb.detectAndCompute(a, None)
    if da is None:
        return None
    best = None
    for flip in (False, True):  # mirrored copies align only after flipping
        bb = cv2.flip(b, 1) if flip else b
        kb, db = _orb.detectAndCompute(bb, None)
        if db is None:
            continue
        good = [m for m, n in (x for x in _bf.knnMatch(da, db, k=2) if len(x) == 2) if m.distance < 0.8 * n.distance]
        if len(good) < 8:
            continue
        H, mask = cv2.findHomography(np.float32([kb[m.trainIdx].pt for m in good]),
                                     np.float32([ka[m.queryIdx].pt for m in good]), cv2.USAC_MAGSAC, 4.0)
        if H is not None and (best is None or mask.sum() > best[0]):
            best = (int(mask.sum()), H, bb)
        if best and best[0] >= 4 * MIN_INLIERS:
            break  # a clear alignment without flipping: skip the mirrored try
    return best


def check(path_a: str, path_b: str) -> dict:
    """{'series': True/False/None, 'share': float|None, 'inliers': int}. None: could not tell."""
    a, b = _gray(path_a), _gray(path_b)
    al = _align(a, b)
    if al is None:
        return dict(series=None, share=None, inliers=0)
    inl, H, bb = al
    hh, ww = a.shape
    w = cv2.warpPerspective(bb, H, (ww, hh))
    m = cv2.warpPerspective(np.full_like(bb, 255), H, (ww, hh)) > 250
    m = cv2.erode(m.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    if inl < MIN_INLIERS or m.mean() < 0.15:
        return dict(series=None, share=None, inliers=inl)

    def norm(x):  # local contrast normalisation: vignettes, glare, tone curves and filters must not count as change
        x = x.astype(np.float32)
        mu = cv2.GaussianBlur(x, (0, 0), 12)
        sd = np.sqrt(cv2.GaussianBlur((x - mu) ** 2, (0, 0), 12)) + 4
        return cv2.GaussianBlur((x - mu) / sd, (0, 0), 1.5)

    A, W = norm(a), norm(w)
    k = np.ones((7, 7), np.uint8)  # ~3 px of slack for imperfect alignment
    d1 = np.maximum(np.maximum(A - cv2.dilate(W, k), cv2.erode(W, k) - A), 0)
    d2 = np.maximum(np.maximum(W - cv2.dilate(A, k), cv2.erode(A, k) - W), 0)
    d = np.minimum(d1, d2)
    d[~m] = 0
    gh, gw = 24, 32
    changed = valid = 0
    for i in range(gh):
        for j in range(gw):
            ys, xs = slice(i * hh // gh, (i + 1) * hh // gh), slice(j * ww // gw, (j + 1) * ww // gw)
            mm = m[ys, xs]
            if mm.mean() > 0.6:
                valid += 1
                changed += np.percentile(d[ys, xs][mm], 75) > CELL_DIFF
    share = changed / max(1, valid)
    return dict(series=bool(share >= MIN_SHARE), share=round(float(share), 3), inliers=inl)
