"""Local demo UI: python -m dedup ui [--port 8777]. Listens on 127.0.0.1 only; photos never leave the Mac."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from .. import features, match, rephoto, series
from ..store import Store

ROOT = Path(__file__).resolve().parents[2]
# the installed app keeps its data in the user's folder (set by packaging/launcher.py); a checkout keeps it in ./data
DATA = Path(os.environ["DEDUP_DATA"]) if os.environ.get("DEDUP_DATA") else ROOT / "data"
UPLOADS = DATA / "uploads"
DB = DATA / "ui.db"
HTML = Path(__file__).with_name("index.html")
TESTS = [p for p in os.environ.get("DEDUP_TEST_REPORTS", "").split(os.pathsep) if p]
_REQ = threading.local()  # the language of the request being answered: the page sends X-Lang: en|ru


def _t(ru: str, en: str) -> str:
    """A message for the page, in the page's language. Without the header (an old page, a script) it stays Russian."""
    return en if getattr(_REQ, "lang", "ru") == "en" else ru


def _store() -> Store:
    DATA.mkdir(exist_ok=True)
    return Store(str(DB))


def _thresholds() -> dict:
    return dict(sscd_copy=match.SSCD_COPY, sscd_geom=match.SSCD_GEOM, sscd_cand=match.SSCD_CAND,
                pdq_max=match.PDQ_MAX, orb_min=match.ORB_MIN_INLIERS)


def _allowed(path: str) -> bool:
    p = os.path.realpath(path)
    if p.startswith(str(UPLOADS.resolve())):
        return True
    roots = [os.path.realpath(os.path.dirname(r)) for r in TESTS]
    extra = [os.path.realpath(x) for x in os.environ.get("DEDUP_IMAGE_ROOTS", "").split(os.pathsep) if x]
    return any(p.startswith(r) for r in roots + extra + sorted(SCANNED_ROOTS))


@lru_cache(maxsize=4096)
def _thumb(path: str, side: int) -> bytes:
    im = Image.open(path)
    im.draft("RGB", (side * 2, side * 2))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((side, side), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _item_view(it: dict, batch_paths: dict, root: str | None) -> dict:
    paths = batch_paths.get(it["sha256"], [])
    path = paths[0] if paths else it["path"]
    return dict(sha=it["sha256"], path=path, paths=paths, name=os.path.basename(path),
                exists=bool(paths) or os.path.exists(path),   # a photo from history may be gone from disk since
                rel=os.path.relpath(path, root) if root and paths else None,
                w=it["width"], h=it["height"], bytes=it.get("size_bytes") or 0, batch=it["batch_id"],
                at=it["ingested_at"], camera=it["camera"], captured=it["captured_at"], new=bool(paths),
                pdq_quality=it["pdq_quality"])


def _batch_time(batch: str) -> datetime:
    m = re.fullmatch(r"b(\d{13})", batch)
    return datetime.fromtimestamp(int(m.group(1)) / 1000).replace(microsecond=0) if m else datetime.now().replace(microsecond=0)


def _sha_cached(st: Store, path: str) -> str:
    stt = os.stat(path)
    sha = st.cached_sha(path, stt.st_size, stt.st_mtime)
    if not sha:
        sha = features.file_sha256(path)
        st.put_file(path, stt.st_size, stt.st_mtime, sha)
    return sha


def _could_be_copy(c: dict) -> bool:
    if c["same_pixels"]:
        return False
    return c["sscd"] >= 0.75 or (c["sscd"] >= 0.5 and c["orb"] >= 5) or (c["pdq_ok"] and c["pdq"] <= 64)


def _mark_series(st: Store, items: list[dict], by_sha: dict, cands: list[dict], progress):
    """For every pair that may be a copy: is it two frames of a series instead? Needs both files; cached per pair."""
    todo = [c for c in cands if _could_be_copy(c)]
    for k, c in enumerate(todo, 1):
        a, b = items[c["i"]], items[c["j"]]
        res = st.get_series(a["sha256"], b["sha256"], series.SERIES_VERSION)
        if res is None:
            pa, pb = (by_sha.get(x["sha256"], [x["path"]])[0] for x in (a, b))
            try:
                res = series.check(pa, pb)
            except Exception:  # a file moved away or unreadable: no verdict, and do not cache it
                res = None
            if res is not None:
                st.put_series(a["sha256"], b["sha256"], series.SERIES_VERSION, res)
        c["series"] = bool(res and res["series"])
        if k % 10 == 0:
            progress("series", k, len(todo))
    st.commit()


def _mark_rephoto(st: Store, items: list[dict], by_sha: dict, new: set, cands: list[dict], progress):
    """New photo vs an old one that is not a copy but shares many details: was the old photo re-shot from a
    screen? Only new-vs-old pairs: that is how an old photo gets passed off as new. Cached per pair and direction."""
    todo = [c for c in cands if (c["i"] in new) != (c["j"] in new) and not c["same_pixels"]
            and 0.25 <= c["sscd"] < 0.75 and c["orb"] >= 30]
    for k, c in enumerate(todo, 1):
        old, nw = (items[c["j"]], items[c["i"]]) if c["i"] in new else (items[c["i"]], items[c["j"]])
        ver = rephoto.REPHOTO_VERSION + (":ab" if old["sha256"] < nw["sha256"] else ":ba")
        res = st.get_series(old["sha256"], nw["sha256"], ver)
        if res is None:
            po, pn = (by_sha.get(x["sha256"], [x["path"]])[0] for x in (old, nw))
            try:
                r = rephoto.check(po, pn)
                res = dict(code=2 if r["crop"] else 1 if r["rephoto"] else 0, share=r["rect"], inliers=r["inliers"])
                st.put_series(old["sha256"], nw["sha256"], ver, res)
            except Exception:  # the old file is gone: nothing to compare with
                res = None
        code = (res or {}).get("code") or 0
        c["rephoto"], c["crop_of_new"] = code == 1, code == 2
        if k % 10 == 0:
            progress("rephoto", k, len(todo))
    st.commit()


def _scan(paths: list[str], batch: str, keep: bool, days: int, root: str | None = None, progress=None) -> dict:
    """Fingerprint new files, compare the batch with itself and with history. days=0: the whole history.
    root: a folder on disk scanned in place (paths stay where they are)."""
    progress = progress or (lambda *a: None)
    st = _store()
    now = datetime.now().replace(microsecond=0)
    t0 = time.perf_counter()
    by_sha: dict[str, list[str]] = {}
    failed = []
    for k, p in enumerate(paths, 1):
        try:
            by_sha.setdefault(_sha_cached(st, p), []).append(p)
        except OSError as e:
            failed.append(dict(name=os.path.basename(p), error=str(e)))
        if k % 50 == 0:
            progress("hash", k, len(paths))
    st.commit()
    seen = [s for s in by_sha if st.known(s)]
    todo = [s for s in by_sha if s not in seen]
    per_photo = []
    for k, s in enumerate(todo, 1):
        try:
            fp = features.compute(by_sha[s][0], sha256=s)
        except Exception as e:
            failed.append(dict(name=os.path.basename(by_sha[s][0]), error=str(e)))
            continue
        st.put(fp, batch, now)
        per_photo.append({key: round(v * 1000, 1) for key, v in fp.timings.items()})
        if k % 20 == 0:
            st.commit()
            progress("fingerprint", k, len(todo))
    st.commit()
    t_fp = time.perf_counter() - t0
    progress("match", 0, 0)
    t1 = time.perf_counter()
    items = st.window(now, days if days > 0 else 365 * 100)
    new_idx = [k for k, it in enumerate(items) if it["sha256"] in by_sha]  # whole batch, incl. known files
    # a big folder is compared all-vs-all: fewer candidates per photo keeps the slow point matching in check
    big = len(new_idx) > 1500
    cands = match.all_candidates(items, new_idx, floor=0.3 if big else 0.2, top_k=15 if big else 50)
    _mark_series(st, items, by_sha, cands, progress)
    _mark_rephoto(st, items, by_sha, set(new_idx), cands, progress)
    t_match = time.perf_counter() - t1
    if not keep:  # one-off check: forget this batch again
        st.db.executemany("DELETE FROM photos WHERE sha256=?", [(s,) for s in todo])
        st.commit()
    used = sorted({c["i"] for c in cands} | {c["j"] for c in cands} | set(new_idx))
    remap = {k: n for n, k in enumerate(used)}
    for c in cands:
        c["i"], c["j"] = remap[c["i"]], remap[c["j"]]
    shas = {items[k]["sha256"] for k in used}
    reviews = {f"{a}|{b}": v for (a, b), v in st.reviews().items() if a in shas and b in shas}
    stages = {}
    for t in per_photo:
        for key, v in t.items():
            stages.setdefault(key, []).append(v)
    last_move = None
    if root:
        r = st.db.execute("SELECT move_id, COUNT(*), MAX(at) FROM moves WHERE root=? AND undone_at IS NULL "
                          "GROUP BY move_id ORDER BY MAX(at) DESC LIMIT 1", (root,)).fetchone()
        if r:
            last_move = dict(id=r[0], files=r[1], at=r[2])
    scope = _scope(root, batch)
    return dict(items=[_item_view(items[k], by_sha, root) for k in used], candidates=cands, reviews=reviews,
                scope=scope, choices=st.choices(scope),
                seen_before=[os.path.basename(by_sha[s][0]) for s in seen],
                same_file=[ps for ps in by_sha.values() if len(ps) > 1],
                failed=failed, thresholds=_thresholds(), window=len(items), window_days=days, kept=keep,
                root=root, batch=batch, batch_at=_batch_time(batch).isoformat(), last_move=last_move,
                timing=dict(photos=len(todo), fingerprint_s=round(t_fp, 2), match_s=round(t_match, 2),
                            per_photo_ms={key: round(float(np.median(v)), 1) for key, v in stages.items()}))


def api_scan(batch: str, keep: bool, days: int = 7) -> dict:
    """A batch uploaded through the browser into data/uploads/<batch>."""
    return _scan(features.list_images(str(UPLOADS / batch)), batch, keep, days)


# ---------- a folder on this Mac, scanned in place, in the background ----------
JOBS: dict[str, dict] = {}
SCANNED_ROOTS: set[str] = set()


def _job_run(job: dict, root: str, keep: bool, days: int):
    def progress(stage, done, total):
        job.update(stage=stage, done=done, total=total)
    try:
        job.update(stage="list", done=0, total=0)
        paths = features.list_images(root)
        job["files"] = len(paths)
        job["result"] = _scan(paths, "dir:" + root, keep, days, root=root, progress=progress)
        job["stage"] = "done"
    except Exception as e:  # shown on the page
        job.update(stage="error", error=str(e))


def api_scan_dir(path: str, keep: bool, days: int) -> dict:
    root = os.path.realpath(os.path.expanduser(path.strip()))
    if not os.path.isdir(root):
        raise ValueError(_t(f"Папка не найдена: {path}", f"Folder not found: {path}"))
    if os.path.basename(root) == features.DUP_DIR:
        raise ValueError(_t("Это папка с уже отложенными дублями, её проверять не нужно",
                            "This is the folder of duplicates already set aside; it does not need checking"))
    if features.in_library(root):
        raise ValueError(_t("Это медиатека приложения «Фото». Её файлы трогать нельзя: удалите дубли в самом приложении",
                            "This is the Apple Photos library. Its files must not be touched: delete duplicates in the Photos app itself"))
    SCANNED_ROOTS.add(root)
    jid = f"j{int(time.time() * 1000)}"
    job = JOBS[jid] = dict(id=jid, root=root, stage="queued", done=0, total=0, started=time.time())
    threading.Thread(target=_job_run, args=(job, root, keep, days), daemon=True).start()
    return dict(job=jid, root=root)


def api_job(jid: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        raise LookupError(_t("Проверка не найдена: страница сервера перезапускалась. Запустите проверку папки снова.",
                             "Check not found: the page server was restarted. Start the folder check again."))
    return {k: v for k, v in job.items() if k != "result"} | ({"result": job["result"]} if job["stage"] == "done" else {})


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath([os.path.realpath(path), root]) == root


def _scope(root: str | None, batch: str) -> str:
    """What a person's keep/remove choices belong to: a folder on disk, or an uploaded batch."""
    return f"dir:{os.path.realpath(root)}" if root else f"batch:{batch}"


def dup_dir_name(name: str | None) -> str:
    """The folder (inside the checked one) that extra files go to. One plain folder name, nothing else."""
    name = (name or features.DUP_DIR).strip()
    if not re.fullmatch(r"[^/\\:\0]{1,60}", name) or name in (".", "..") or name.startswith("."):
        raise ValueError(_t("Имя папки для лишних файлов: одно слово или несколько, без / \\ : и без точки в начале",
                            "Folder name for extra files: one or more words, without / \\ : and without a dot at the start"))
    return name


def api_choose_folder() -> dict:
    """Open the macOS folder chooser on this Mac and return the picked folder. A browser never tells a page the real
    path of a folder it picked, so the page asks the server, which runs on the same Mac, to show the system dialog."""
    prompt = _t("Выберите папку с фото для проверки", "Choose a folder of photos to check")
    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Windows.Forms; $f = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
              f"$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = '{prompt}'; "
              "if ($d.ShowDialog($f) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $d.SelectedPath } else { exit 3 }")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, timeout=900,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 3:  # the person pressed Cancel
            return dict(cancelled=True)
        path = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode != 0 or not path:
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or _t("окно выбора папки не открылось", "the folder picker did not open"))
        return dict(path=path)
    if sys.platform != "darwin":
        raise RuntimeError(_t("Окно выбора папки работает только на Mac и Windows", "The folder picker works only on Mac and Windows"))
    r = subprocess.run(["osascript", "-e", "activate",
                        "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
                       capture_output=True, text=True, timeout=900)
    print(_t("выбор папки: код", "folder picker: code"), f"{r.returncode}, {r.stdout.strip() or r.stderr.strip()[:120]}", flush=True)
    if r.returncode != 0:
        if "-128" in r.stderr:  # the person pressed Cancel
            return dict(cancelled=True)
        raise RuntimeError(r.stderr.strip() or _t("окно выбора папки не открылось", "the folder picker did not open"))
    return dict(path=r.stdout.strip().rstrip("/") or "/")


def api_choices(scope: str, choices: dict) -> dict:
    st = _store()
    now = datetime.now().replace(microsecond=0)
    for sha, c in choices.items():
        st.set_choice(scope, sha, c, now)
    st.commit()
    return dict(ok=True, n=len(choices))


def api_move(root: str, files: list[dict], dup_name: str | None = None) -> dict:
    """Move the files a person marked "remove" into <root>/<dup_name>/<same relative path>. Nothing is deleted.
    files: [{path, sha, forget}] — forget=True drops the fingerprint, so history does not keep matching it."""
    root = os.path.realpath(root)
    if root not in SCANNED_ROOTS:
        raise PermissionError(_t("Эту папку сначала нужно проверить на этой странице", "This folder must first be checked on this page"))
    dup = os.path.join(root, dup_dir_name(dup_name))
    # never pour extra files into a folder of the person's own photos: the mark would also hide it from later scans
    if os.path.isdir(dup) and not os.path.exists(os.path.join(dup, features.DUP_MARK)) \
            and any(e not in (".DS_Store",) for e in os.listdir(dup)):
        raise ValueError(_t(f"В проверенной папке уже есть папка «{os.path.basename(dup)}» с вашими файлами. "
                            "Выберите в профиле другое имя для папки лишних файлов",
                            f"The checked folder already has a folder «{os.path.basename(dup)}» with your files. "
                            "Choose another name for the extra-files folder in the profile"))
    st = _store()
    now = datetime.now().replace(microsecond=0)
    mid = f"m{int(time.time() * 1000)}"
    moved, size, failed = 0, 0, []
    for f in files:
        src = os.path.realpath(f["path"])
        rid = dst = None
        try:
            if not _inside(src, root) or _inside(src, dup) or not os.path.isfile(src):
                raise ValueError(_t("файл не внутри проверенной папки или уже перемещён",
                                    "the file is not inside the checked folder or has already been moved"))
            if features.in_library(src):
                raise ValueError(_t("файл внутри медиатеки «Фото»: перенос сломал бы медиатеку",
                                    "the file is inside the Photos library: moving it would break the library"))
            if f.get("sha") and _sha_cached(st, src) != f["sha"]:
                raise ValueError(_t("файл изменился после проверки, проверьте папку заново",
                                    "the file changed after the check, check the folder again"))
            dst = os.path.join(dup, os.path.relpath(src, root))
            base, ext = os.path.splitext(dst)
            k = 1
            while os.path.exists(dst):
                dst, k = f"{base} ({k}){ext}", k + 1
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(os.path.join(dup, features.DUP_MARK), "a").close()
            n = os.path.getsize(src)
            # write down where the file goes before moving it: a crash halfway still leaves "Вернуть" and the
            # text log knowing about every moved file
            with open(os.path.join(dup, "журнал.txt"), "a", encoding="utf-8") as log:
                log.write(f"{now.isoformat()} {mid}  {src}  ->  {dst}\n")
            rid = st.db.execute("INSERT INTO moves (move_id, root, src, dst, sha256, at) VALUES (?,?,?,?,?,?)",
                                (mid, root, src, dst, f.get("sha"), now.isoformat())).lastrowid
            st.commit()
            shutil.move(src, dst)
            moved, size = moved + 1, size + n
            if f.get("forget") and f.get("sha"):
                st.db.execute("DELETE FROM photos WHERE sha256=?", (f["sha"],))
                st.commit()
        except Exception as e:
            if rid is not None and dst and os.path.exists(dst) and not os.path.exists(src):
                continue  # the file did move; only forgetting its fingerprint failed
            if rid is not None:  # recorded, but the file stayed where it was
                st.db.execute("DELETE FROM moves WHERE id=?", (rid,))
                st.commit()
            failed.append(dict(path=f["path"], error=str(e)))
    st.commit()
    return dict(move_id=mid, moved=moved, bytes=size, failed=failed, dup_dir=dup)


def api_move_undo(mid: str) -> dict:
    st = _store()
    rows = st.db.execute("SELECT id, src, dst FROM moves WHERE move_id=? AND undone_at IS NULL", (mid,)).fetchall()
    back, failed = 0, []
    now = datetime.now().replace(microsecond=0).isoformat()
    for rid, src, dst in rows:
        try:
            if os.path.exists(src) and not os.path.exists(dst):  # never left its place (a crash before the move)
                st.db.execute("UPDATE moves SET undone_at=? WHERE id=?", (now, rid))
                back += 1
                continue
            if os.path.exists(src):
                raise FileExistsError(_t("на старом месте уже лежит другой файл", "another file is already in the old place"))
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dst, src)
            st.db.execute("UPDATE moves SET undone_at=? WHERE id=?", (now, rid))
            back += 1
        except Exception as e:
            failed.append(dict(path=src, error=str(e)))
    st.commit()
    return dict(returned=back, failed=failed)


def api_review(pairs: list[list[str]], verdict: str | None) -> dict:
    st = _store()
    now = datetime.now().replace(microsecond=0)
    for a, b in pairs:
        st.set_review(a, b, verdict, now)
    st.commit()
    return dict(ok=True, n=len(pairs))


PRESETS = DATA / "presets.json"


def _attack(im: Image.Image, o: dict) -> Image.Image:
    w, h = im.size
    if o.get("crop"):
        k = (1 - float(o["crop"]) / 100) ** 0.5
        cw, ch = round(w * k), round(h * k)
        x, y = (w - cw) // 2, (h - ch) // 2
        im = im.crop((x, y, x + cw, y + ch))
    if o.get("edge"):
        im = im.crop((0, 0, round(im.width * (1 - float(o["edge"]) / 100)), im.height))
    if o.get("rotate"):
        im = im.rotate(-float(o["rotate"]), resample=Image.BICUBIC, expand=False)
    if o.get("tilt"):
        t = float(o["tilt"]) / 100
        W, H = im.size
        im = im.transform((W, H), Image.QUAD, (W * t, 0, 0, H, W, H, W * (1 - t), 0), Image.BICUBIC)
    if o.get("mirror"):
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    if o.get("bright"):
        im = ImageEnhance.Brightness(im).enhance(1 + float(o["bright"]) / 100)
    if o.get("text"):
        d = ImageDraw.Draw(im)
        size = max(14, im.height // 9)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", size)
        except OSError:
            font = ImageFont.load_default()
        d.text((im.width * 0.06, im.height * 0.72), str(o["text"])[:40], fill=(255, 255, 255), font=font,
               stroke_width=max(1, size // 12), stroke_fill=(0, 0, 0))
    if o.get("scale") and float(o["scale"]) < 100:
        s = float(o["scale"]) / 100
        im = im.resize((max(8, round(im.width * s)), max(8, round(im.height * s))), Image.BILINEAR)
    return im


def api_lab(path: str, ops: dict) -> dict:
    if not _allowed(path):
        raise PermissionError("path not allowed")
    src = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    src.thumbnail((1600, 1600), Image.BILINEAR)
    att = _attack(src, ops)
    tmp = Path(tempfile.mkdtemp(prefix="dedup_lab_"))
    try:
        a_path, b_path = tmp / "a.jpg", tmp / "b.jpg"
        src.save(a_path, quality=95)
        att.save(b_path, quality=int(ops.get("jpeg") or 92))
        fa, fb = features.compute(str(a_path)), features.compute(str(b_path))
        items = []
        for fp in (fa, fb):
            items.append(dict(sha256=fp.sha256, pixel_sha=fp.pixel_sha, pdq=fp.pdq, pdq_quality=fp.pdq_quality,
                              sscd=fp.sscd, orb_xy=fp.orb_xy, orb_des=fp.orb_des, captured_at=None, camera=None))
        c = match.all_candidates(items, [1], floor=-1.0)[0]
        plain = int(np.unpackbits(np.bitwise_xor(fa.pdq[0], fb.pdq[0])).sum())
        buf = io.BytesIO()
        Image.open(b_path).convert("RGB").resize(
            (min(640, att.width), round(att.height * min(640, att.width) / att.width))).save(buf, "JPEG", quality=85)
        return dict(candidate=c, pdq_plain=plain, pdq_quality=[fa.pdq_quality, fb.pdq_quality],
                    size=[att.width, att.height], file_kb=round(os.path.getsize(b_path) / 1024, 1),
                    variant="data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
                    timing_ms={k: round(v * 1000, 1) for k, v in fb.timings.items()}, thresholds=_thresholds())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def api_history() -> dict:
    st = _store()
    rows = st.db.execute("SELECT sha256, path, batch_id, ingested_at, width, height, camera, captured_at "
                         "FROM photos ORDER BY ingested_at DESC").fetchall()
    pairs = st.db.execute("SELECT COUNT(*), verdict FROM pairs GROUP BY verdict").fetchall()
    return dict(photos=[dict(sha=r[0][:12], path=r[1], name=os.path.basename(r[1]), batch=r[2], at=r[3],
                             w=r[4], h=r[5], camera=r[6], captured=r[7]) for r in rows],
                db_bytes=DB.stat().st_size if DB.exists() else 0, pairs={v: n for n, v in pairs})


@lru_cache(maxsize=1)
def _name_index() -> dict:
    idx = {}
    for r in [x for x in os.environ.get("DEDUP_IMAGE_ROOTS", "").split(os.pathsep) if x]:
        for f in features.list_images(r):
            idx.setdefault(os.path.basename(f), f)
    return idx


def api_tests() -> dict:
    out = []
    idx = _name_index()
    for r in TESTS:
        p = Path(r)
        if p.exists():
            rep = json.loads(p.read_text())
            missed = rep.get("attacks", {}).get("missed_examples", {})
            rep.setdefault("attacks", {})["missed_paths"] = {
                k: [idx[n] for n in v if n in idx] for k, v in missed.items()}
            out.append(dict(name=p.parent.name, report=rep))
    return dict(reports=out)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if ctype == "image/jpeg" else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _lang(self):
        _REQ.lang = "en" if (self.headers.get("X-Lang") or "").lower() == "en" else "ru"

    def do_GET(self):
        self._lang()
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/img":
                if not _allowed(q["path"]):
                    return self._json({"error": "path not allowed"}, 403)
                return self._send(200, _thumb(q["path"], int(q.get("side", 320))), "image/jpeg")
            if u.path == "/api/job":
                return self._json(api_job(q["id"]))
            if u.path == "/api/presets":
                return self._json(json.loads(PRESETS.read_text()) if PRESETS.exists() else {"presets": []})
            if u.path == "/api/history":
                return self._json(api_history())
            if u.path == "/api/tests":
                return self._json(api_tests())
            if u.path == "/api/samples":
                files = []
                for r in [UPLOADS] + [Path(x) for x in os.environ.get("DEDUP_IMAGE_ROOTS", "").split(os.pathsep) if x]:
                    if r.exists():
                        files += features.list_images(str(r))[:400]
                return self._json(dict(files=[dict(path=f, name=os.path.basename(f)) for f in files]))
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _local(self) -> bool:
        # Pages from other sites cannot send this header without a CORS pre-flight, which this server never answers;
        # the Host check stops DNS rebinding. Together they keep other sites from moving files through this page.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return self.headers.get("X-Dedup") == "1" and host in ("127.0.0.1", "localhost")

    def do_POST(self):
        self._lang()
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if not self._local():
            return self._json({"error": _t("запрос не со страницы проверки", "the request did not come from the check page")}, 403)
        try:
            if u.path == "/api/upload":
                batch = re.sub(r"[^\w.-]", "_", q["batch"])
                name = re.sub(r"[^\w.\- ()]", "_", os.path.basename(q["name"]))[:120]
                d = UPLOADS / batch
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(self._body())
                return self._json({"ok": True})
            if u.path == "/api/scan":
                batch = re.sub(r"[^\w.-]", "_", q["batch"])
                return self._json(api_scan(batch, q.get("keep") == "1", int(q.get("days", "7"))))
            if u.path == "/api/scan_dir":
                b = json.loads(self._body() or b"{}")
                return self._json(api_scan_dir(b["path"], bool(b.get("keep")), int(b.get("days", 0))))
            if u.path == "/api/move":
                b = json.loads(self._body() or b"{}")
                return self._json(api_move(b["root"], b["files"], b.get("dup_dir")))
            if u.path == "/api/move/undo":
                b = json.loads(self._body() or b"{}")
                return self._json(api_move_undo(b["move_id"]))
            if u.path == "/api/choose_folder":
                return self._json(api_choose_folder())
            if u.path == "/api/quit":  # the "Выключить программу" button: the installed app has no window to close
                threading.Timer(0.3, self.server.shutdown).start()
                return self._json({"ok": True})
            if u.path == "/api/choices":
                b = json.loads(self._body() or b"{}")
                return self._json(api_choices(str(b["scope"]), b.get("choices") or {}))
            if u.path == "/api/review":
                b = json.loads(self._body() or b"{}")
                return self._json(api_review(b["pairs"], b.get("verdict")))
            if u.path == "/api/presets":
                b = json.loads(self._body() or b"{}")
                DATA.mkdir(exist_ok=True)
                PRESETS.write_text(json.dumps({"presets": b.get("presets", [])}, ensure_ascii=False, indent=1))
                return self._json({"ok": True})
            if u.path == "/api/lab":
                b = json.loads(self._body() or b"{}")
                return self._json(api_lab(b["path"], b.get("ops", {})))
            if u.path == "/api/history/clear":
                st = _store()
                n = st.db.execute("DELETE FROM photos").rowcount
                st.db.execute("DELETE FROM pairs")
                st.db.execute("DELETE FROM files")
                st.commit()
                shutil.rmtree(UPLOADS, ignore_errors=True)
                return self._json({"deleted": n})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


def _ours(port: int) -> bool:
    """Is this program already answering on the port? Then a second start only opens the page."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.5) as r:
            return "Duplicate Photo Finder" in r.read(4000).decode("utf-8", "replace")
    except Exception:
        return False


def serve(port: int = 8777, open_browser: bool = False):
    import webbrowser
    DATA.mkdir(parents=True, exist_ok=True)
    srv = None
    for p in range(port, port + 20):
        if _ours(p):
            print(f"Программа уже запущена: http://127.0.0.1:{p}/\nThe program is already running: http://127.0.0.1:{p}/", flush=True)
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{p}/")
            return
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            port = p
            break
        except OSError:  # the port is taken by another program: try the next one
            continue
    if srv is None:
        raise SystemExit(f"Не нашлось свободного порта от {port} до {port + 19}\nNo free port from {port} to {port + 19}")
    url = f"http://127.0.0.1:{port}/"
    # before any page is open the language is unknown: both lines
    print(f"Открыть: {url}  (остановить: кнопка «Выключить» на странице или Ctrl+C)\n"
          f"Open: {url}  (to stop: the \"Shut down\" button on the page, or Ctrl+C)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Программа выключена\nThe program is shut down", flush=True)
