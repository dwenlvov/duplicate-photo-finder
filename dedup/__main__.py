"""CLI: python -m dedup scan <folder> [--db dedup.db] [--now 2026-11-13T12:00] [--json out.json]"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime

from . import features, match
from .store import Store


def scan(folder: str, db: str, now: datetime, days: int, batch_id: str, quiet: bool = False) -> dict:
    t_start = time.perf_counter()
    st = Store(db)
    paths = features.list_images(folder)

    by_sha: dict[str, list[str]] = defaultdict(list)
    t0 = time.perf_counter()
    for p in paths:
        by_sha[features.file_sha256(p)].append(p)
    t_sha = time.perf_counter() - t0

    seen_before = {s: ps for s, ps in by_sha.items() if st.known(s)}
    todo = [s for s in by_sha if s not in seen_before]
    stage = defaultdict(float)
    failed = []
    for k, s in enumerate(todo, 1):
        try:
            fp = features.compute(by_sha[s][0], sha256=s)
        except Exception as e:  # unreadable / unsupported file
            failed.append((by_sha[s][0], repr(e)))
            continue
        for key, v in fp.timings.items():
            stage[key] += v
        st.put(fp, batch_id, now)
        if not quiet and k % 200 == 0:
            print(f"  {k}/{len(todo)} fingerprinted", file=sys.stderr, flush=True)
    st.commit()
    t_fp = time.perf_counter() - t0 - t_sha

    t0 = time.perf_counter()
    items = st.window(now, days)
    batch_shas = set(todo)
    new_idx = [k for k, it in enumerate(items) if it["sha256"] in batch_shas]
    pairs = match.find_pairs(items, new_idx)
    for p in pairs:
        st.put_pair(items[p.i]["sha256"], items[p.j]["sha256"], p.verdict, p.reason, p.sscd, p.pdq, p.orb, now)
    st.commit()
    t_match = time.perf_counter() - t0

    def show(k):
        it = items[k]
        tag = "" if it["sha256"] in batch_shas else f"  [история: {it['ingested_at'][:10]}]"
        return it["path"] + tag

    copy_groups = [[show(k) for k in g] for g in match.groups(len(items), pairs) if any(k in set(new_idx) for k in g)]
    similar = [(show(p.i), show(p.j), round(p.sscd, 3), p.orb, p.reason) for p in pairs if p.verdict == "similar"]
    same_file = [ps for ps in by_sha.values() if len(ps) > 1]
    res = {
        "files": len(paths), "unique_files": len(by_sha), "new_fingerprinted": len(todo) - len(failed),
        "seen_before": {s: ps for s, ps in seen_before.items()}, "same_file_in_batch": same_file,
        "failed": failed, "window_size": len(items),
        "pairs": [dict(a=items[p.i]["path"], b=items[p.j]["path"], verdict=p.verdict, reason=p.reason,
                       sscd=round(p.sscd, 4), pdq=p.pdq, orb=p.orb) for p in pairs],
        "copy_groups": copy_groups, "similar_pairs": similar,
        "timing_s": {"total": round(time.perf_counter() - t_start, 2), "sha": round(t_sha, 2),
                     "fingerprint": round(t_fp, 2), "match": round(t_match, 2),
                     **{f"stage_{k}": round(v, 2) for k, v in stage.items()}},
    }
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dedup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="fingerprint a folder and find duplicates in it and in the last N days")
    sc.add_argument("folder")
    sc.add_argument("--db", default="dedup.db")
    sc.add_argument("--now", help="ISO time for this batch (default: now)")
    sc.add_argument("--days", type=int, default=7)
    sc.add_argument("--batch-id")
    sc.add_argument("--json", help="write full result here")
    sc.add_argument("--quiet", action="store_true")
    ui = sub.add_parser("ui", help="local demo page on http://127.0.0.1:8777")
    ui.add_argument("--port", type=int, default=8777)
    ui.add_argument("--open", action="store_true", help="open the page in the browser")
    a = ap.parse_args(argv)

    if a.cmd == "ui":
        from .ui.server import serve

        serve(a.port, a.open)
        return
    now =datetime.fromisoformat(a.now) if a.now else datetime.now().replace(microsecond=0)
    res = scan(a.folder, a.db, now, a.days, a.batch_id or now.isoformat(), a.quiet)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    if a.quiet:
        return
    print(f"Файлов: {res['files']}, новых отпечатков: {res['new_fingerprinted']}, "
          f"уже были в базе: {len(res['seen_before'])}, в окне {a.days} дн.: {res['window_size']}")
    for ps in res["same_file_in_batch"]:
        print("Один и тот же файл:", *ps, sep="\n  ")
    for i, g in enumerate(res["copy_groups"], 1):
        print(f"Копии, группа {i}:", *g, sep="\n  ")
    for a_, b_, s, o, r in res["similar_pairs"]:
        print(f"Похожие (sscd={s}, orb={o}, {r}):\n  {a_}\n  {b_}")
    for p, e in res["failed"]:
        print("Не прочитан:", p, e)
    print("Время, с:", res["timing_s"])


if __name__ == "__main__":
    main()
