"""Per-photo fingerprint: everything we store so a photo is never decoded twice."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import pdqhash
from PIL import Image, ImageOps

try:  # HEIC/HEIF from iPhones; optional
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

# Bump when any feature computation changes: stored records with another version get recomputed.
FEATURE_VERSION = "f2-draft1024-pdq512-sscdint8_288-orb1000_800"

WORK_SIDE = 1024      # decode straight to roughly this long side (JPEG DCT scaling), then resize
SSCD_SHORT = 288      # SSCD input: short side
ORB_LONG = 800        # ORB input: long side
ORB_N = 1000
PDQ_SIDE = 512       # PDQ downsamples internally; 512 px input is enough

_MEAN = np.array([0.485, 0.456, 0.406], np.float32).reshape(3, 1, 1)
_STD = np.array([0.229, 0.224, 0.225], np.float32).reshape(3, 1, 1)
_MODEL = Path(__file__).with_name("models") / "sscd_int8.onnx"
_sess: ort.InferenceSession | None = None
_orb = cv2.ORB_create(nfeatures=ORB_N)

DUP_DIR = "_дубли"   # default folder the UI moves extra files into; never scanned again
DUP_MARK = ".dedup_moved_here"   # put into every such folder, whatever its name, so scans skip it
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}


def _session() -> ort.InferenceSession:
    global _sess
    if _sess is None:
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _sess = ort.InferenceSession(str(_MODEL), so, providers=["CPUExecutionProvider"])
    return _sess


@dataclass
class Fingerprint:
    sha256: str
    path: str
    size_bytes: int
    width: int                 # original size after EXIF orientation
    height: int
    pixel_sha: str             # hash of decoded (draft) pixels: same picture with other metadata
    pdq: np.ndarray            # (8, 32) uint8: PDQ hash for 8 rotations/flips; row 0 = as is
    pdq_quality: int
    sscd: np.ndarray           # (512,) float16, L2-normalised
    orb_xy: np.ndarray         # (n, 2) float32
    orb_des: np.ndarray        # (n, 32) uint8
    captured_at: str | None = None   # EXIF DateTimeOriginal + SubSec
    camera: str | None = None        # Make Model (+ serial when present)
    timings: dict = field(default_factory=dict)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _exif_meta(im: Image.Image) -> tuple[str | None, str | None]:
    try:
        ex = im.getexif()
        sub = ex.get_ifd(0x8769)  # Exif IFD
    except Exception:
        return None, None
    dt = sub.get(0x9003)                         # DateTimeOriginal
    ss = sub.get(0x9291)                         # SubSecTimeOriginal
    make, model = ex.get(0x010F), ex.get(0x0110)
    serial = sub.get(0xA431)                     # BodySerialNumber
    cap = f"{dt}.{ss}" if dt and ss else (str(dt) if dt else None)
    cam = " ".join(str(x).strip() for x in (make, model, serial) if x) or None
    return cap, cam


def _pdq_rows(rgb: np.ndarray) -> tuple[np.ndarray, int]:
    hashes, q = pdqhash.compute_dihedral(rgb)
    # library order: original, rot90, rot180, rot270, flipX, flipY, flipPlus1, flipMinus1
    rows = np.stack([np.packbits(np.asarray(h, dtype=np.uint8)) for h in hashes])
    return rows, int(q)


def _sscd(im: Image.Image) -> np.ndarray:
    w, h = im.size
    s = SSCD_SHORT / min(w, h)
    x = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BILINEAR)
    x = ((np.asarray(x, dtype=np.float32) / 255.0).transpose(2, 0, 1) - _MEAN) / _STD
    e = _session().run(None, {"x": x[None]})[0][0]
    return (e / np.linalg.norm(e)).astype(np.float16)


def orb_features(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h, w = gray.shape
    s = min(1.0, ORB_LONG / max(h, w))
    if s < 1:
        gray = cv2.resize(gray, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    kp, des = _orb.detectAndCompute(gray, None)
    if des is None:
        return np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)
    return np.float32([k.pt for k in kp]).reshape(-1, 2), des


def compute(path: str, sha256: str | None = None) -> Fingerprint:
    import time

    t = {}
    t0 = time.perf_counter()
    sha = sha256 or file_sha256(path)
    t["sha"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    im = Image.open(path)
    captured, camera = _exif_meta(im)
    # Decode directly at reduced size where the format allows it (JPEG DCT scaling).
    im.draft("RGB", (WORK_SIDE, WORK_SIDE))
    im = ImageOps.exif_transpose(im)
    im = im.convert("RGB")
    # Original geometry (after orientation), from the header, not the draft size
    ow, oh = Image.open(path).size
    o = (Image.open(path).getexif() or {}).get(0x0112, 1)
    if o in (5, 6, 7, 8):
        ow, oh = oh, ow
    if max(im.size) > WORK_SIDE:
        im.thumbnail((WORK_SIDE, WORK_SIDE), Image.BILINEAR)
    rgb = np.asarray(im)
    t["decode"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    pixel_sha = hashlib.sha256(rgb.tobytes() + f"{ow}x{oh}".encode()).hexdigest()
    small = im.copy()
    small.thumbnail((PDQ_SIDE, PDQ_SIDE), Image.BILINEAR)
    pdq, pdq_q = _pdq_rows(np.asarray(small))
    t["hashes"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    sscd = _sscd(im)
    t["sscd"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    xy, des = orb_features(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
    t["orb"] = time.perf_counter() - t0

    return Fingerprint(sha, os.path.abspath(path), os.path.getsize(path), ow, oh, pixel_sha, pdq, pdq_q,
                       sscd, xy, des, captured, camera, t)


LIBRARY_EXT = (".photoslibrary", ".photolibrary", ".aplibrary", ".migratedphotolibrary")  # Photos, iPhoto, Aperture


def in_library(path: str) -> bool:
    """Inside a photo app's library package: moving a file out of it breaks the library."""
    return any(part.lower().endswith(LIBRARY_EXT) for part in Path(path).parts)


def list_images(folder: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        # skip hidden folders, photo app libraries and folders the page moved extra files into (any name: DUP_MARK)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != DUP_DIR and not d.lower().endswith(LIBRARY_EXT)
                   and not os.path.exists(os.path.join(root, d, DUP_MARK))]
        for f in files:
            if Path(f).suffix.lower() in IMAGE_EXT and not f.startswith("."):
                out.append(os.path.join(root, f))
    return sorted(out)


def parse_captured(s: str | None) -> datetime | None:
    if not s:
        return None
    base, _, sub = s.partition(".")
    try:
        d = datetime.strptime(base.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if sub.strip().isdigit():
        d = d.replace(microsecond=int(sub.strip()[:6].ljust(6, "0")))
    return d
