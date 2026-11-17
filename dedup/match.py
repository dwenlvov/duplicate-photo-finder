"""Pair decisions: exact -> copy -> similar. Thresholds are starting points from the research reports."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from . import series as _series
from .features import parse_captured

# --- thresholds (see reports/; calibrate on your own labelled pairs) ---
SSCD_COPY = 0.75        # strong copy signal (SSCD paper: ~90% precision on DISC21)
SSCD_GEOM = 0.60        # copy only if geometry confirms
SSCD_CAND = 0.33        # below: different photos. Above, not a copy: similar (Holidays 2026-11-15: 66% of same-place pairs, 64 of 1488 photos with a false one)
PDQ_MAX = 31            # Meta's recommended match distance (of 256 bits)
PDQ_MIN_QUALITY = 50
ORB_MIN_INLIERS = 13    # random pairs reached at most 12 in our test
TOP_K = 30              # candidates per new photo (guards against burst floods)
BURST_MAX_GAP_S = 3600  # same camera, different capture time within an hour -> a different shot

_bf = cv2.BFMatcher(cv2.NORM_HAMMING)


@dataclass
class Pair:
    i: int
    j: int
    verdict: str        # exact | copy | similar
    reason: str
    sscd: float
    pdq: int
    orb: int


def orb_inliers(a_xy, a_des, b_xy, b_des) -> int:
    if len(a_des) < 8 or len(b_des) < 8:
        return 0
    good = [m for m, n in (x for x in _bf.knnMatch(a_des, b_des, k=2) if len(x) == 2) if m.distance < 0.8 * n.distance]
    if len(good) < 8:
        return 0
    H, mask = cv2.findHomography(a_xy[[m.queryIdx for m in good]], b_xy[[m.trainIdx for m in good]], cv2.USAC_MAGSAC, 5.0)
    return int(mask.sum()) if mask is not None else 0


def _bits(pdq_rows: np.ndarray) -> np.ndarray:
    return np.unpackbits(pdq_rows, axis=-1).astype(np.float32)  # (..., 256)


def _different_shot(a: dict, b: dict) -> bool:
    """Same camera, both with capture time, times differ: two exposures, so not a copy of each other."""
    if not (a.get("camera") and a.get("camera") == b.get("camera")):
        return False
    ta, tb = parse_captured(a.get("captured_at")), parse_captured(b.get("captured_at"))
    if ta is None or tb is None or ta == tb:
        return False
    return abs((ta - tb).total_seconds()) <= BURST_MAX_GAP_S


def _is_series(pa: str, pb: str) -> bool:
    try:
        return bool(_series.check(pa, pb)["series"])
    except Exception:
        return False


def all_candidates(items: list[dict], new_idx: list[int], floor: float = 0.2, top_k: int = 50) -> list[dict]:
    """Every pair worth showing in the UI, with all raw scores (ORB always computed), no verdict.
    The UI applies thresholds itself, so sliders re-classify pairs without recomputing."""
    n = len(items)
    if n < 2 or not new_idx:
        return []
    S, D, q_ok, is_new = _matrices(items, new_idx)
    out = {}
    for qi, i in enumerate(new_idx):
        mask = np.ones(n, bool)
        mask[i] = False
        mask &= ~(is_new & (np.arange(n) < i))
        cand = np.where(mask & ((S[qi] >= floor) | (D[qi] <= 64)))[0]
        exact = [j for j in np.where(mask)[0] if items[j]["pixel_sha"] == items[i]["pixel_sha"]]
        cand = cand[np.argsort(-S[qi][cand])][:top_k]
        for j in set(cand.tolist()) | set(exact):
            a, b = items[i], items[j]
            key = (min(i, j), max(i, j))
            out[key] = dict(i=key[0], j=key[1], sscd=round(float(S[qi, j]), 4), pdq=int(D[qi, j]),
                            pdq_ok=bool(q_ok[i] and q_ok[j]), same_pixels=a["pixel_sha"] == b["pixel_sha"],
                            orb=orb_inliers(a["orb_xy"], a["orb_des"], b["orb_xy"], b["orb_des"]),
                            different_shot=_different_shot(a, b))
    return list(out.values())


def _matrices(items, new_idx):
    n = len(items)
    E = np.stack([it["sscd"] for it in items]).astype(np.float32)
    P0 = _bits(np.stack([it["pdq"][0] for it in items]))
    Pd = _bits(np.stack([it["pdq"] for it in items])[new_idx])
    q_ok = np.array([it["pdq_quality"] >= PDQ_MIN_QUALITY for it in items])
    is_new = np.zeros(n, bool)
    is_new[new_idx] = True
    S = E[new_idx] @ E.T
    ones0 = P0.sum(1)
    D = np.full(S.shape, 256, np.int32)
    for r in range(8):
        Q = Pd[:, r, :]
        h = Q.sum(1)[:, None] + ones0[None, :] - 2 * (Q @ P0.T)
        D = np.minimum(D, np.rint(h).astype(np.int32))
    return S, D, q_ok, is_new


def find_pairs(items: list[dict], new_idx: list[int]) -> list[Pair]:
    """items: all records (history window + new batch). new_idx: positions of the new batch."""
    n = len(items)
    if n < 2 or not new_idx:
        return []
    E = np.stack([it["sscd"] for it in items]).astype(np.float32)
    P0 = _bits(np.stack([it["pdq"][0] for it in items]))                    # (n,256) as-is
    Pd = _bits(np.stack([it["pdq"] for it in items])[new_idx])              # (q,8,256) dihedral of queries
    q_ok = np.array([it["pdq_quality"] >= PDQ_MIN_QUALITY for it in items])
    is_new = np.zeros(n, bool)
    is_new[new_idx] = True

    S = E[new_idx] @ E.T                                                     # (q,n) cosine
    ones0 = P0.sum(1)
    # Hamming via dot products, min over the 8 rotations/flips of the query
    D = np.full(S.shape, 256, np.int32)
    for r in range(8):
        Q = Pd[:, r, :]
        h = Q.sum(1)[:, None] + ones0[None, :] - 2 * (Q @ P0.T)
        D = np.minimum(D, np.rint(h).astype(np.int32))

    pairs: dict[tuple[int, int], Pair] = {}
    for qi, i in enumerate(new_idx):
        s_row, d_row = S[qi], D[qi]
        mask = np.ones(n, bool)
        mask[i] = False
        mask &= ~(is_new & (np.arange(n) < i))                               # each new-new pair once
        cand = np.where(mask & ((s_row >= SSCD_CAND) | ((d_row <= PDQ_MAX) & q_ok & q_ok[i])))[0]
        exact = [j for j in np.where(mask)[0] if items[j]["pixel_sha"] == items[i]["pixel_sha"]]
        cand = cand[np.argsort(-s_row[cand])][:TOP_K]
        for j in set(cand.tolist()) | set(exact):
            a, b = items[i], items[j]
            s, d = float(s_row[j]), int(d_row[j])
            pdq_hit = d <= PDQ_MAX and q_ok[i] and q_ok[j]
            orb = -1
            if a["pixel_sha"] == b["pixel_sha"]:
                verdict, reason = "exact", "same pixels"
            elif s >= SSCD_COPY:
                verdict, reason = "copy", "sscd"
            elif pdq_hit:
                verdict, reason = "copy", "pdq"
            else:
                orb = orb_inliers(a["orb_xy"], a["orb_des"], b["orb_xy"], b["orb_des"])
                if orb >= ORB_MIN_INLIERS and s >= SSCD_GEOM:
                    verdict, reason = "copy", "sscd+orb"
                elif orb >= ORB_MIN_INLIERS:
                    verdict, reason = "similar", "orb"
                elif s >= SSCD_CAND:   # Holidays: same place, other shot: 70% of pairs at >= 0.30, few false ones
                    verdict, reason = "similar", "sscd"
                else:
                    continue
            if verdict == "copy" and _different_shot(a, b):
                verdict, reason = "similar", reason + "; exif: different shot"
            elif verdict == "copy" and a.get("path") and b.get("path") and _is_series(a["path"], b["path"]):
                verdict, reason = "similar", reason + "; series: content moved"
            key = (min(i, j), max(i, j))
            pairs[key] = Pair(key[0], key[1], verdict, reason, s, d, orb)
    return list(pairs.values())


def groups(n: int, pairs: list[Pair], kinds=("exact", "copy")) -> list[list[int]]:
    parent = list(range(n))

    def f(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        if p.verdict in kinds:
            parent[f(p.i)] = f(p.j)
    g: dict[int, list[int]] = {}
    for x in range(n):
        g.setdefault(f(x), []).append(x)
    return [v for v in g.values() if len(v) > 1]
