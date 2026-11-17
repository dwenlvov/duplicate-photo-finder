"""SQLite store: one row per photo (keyed by file SHA-256) + found pairs. Rolling window by ingest time."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import numpy as np

from .features import FEATURE_VERSION, Fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    sha256       TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    size_bytes   INTEGER,
    width        INTEGER,
    height       INTEGER,
    pixel_sha    TEXT NOT NULL,
    pdq          BLOB NOT NULL,      -- 8 x 32 bytes
    pdq_quality  INTEGER,
    sscd         BLOB NOT NULL,      -- 512 x float16
    orb_xy       BLOB NOT NULL,
    orb_des      BLOB NOT NULL,
    captured_at  TEXT,
    camera       TEXT,
    batch_id     TEXT,
    ingested_at  TEXT NOT NULL,      -- ISO, drives the 7-day window
    feature_ver  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS photos_ingested ON photos(ingested_at);
CREATE INDEX IF NOT EXISTS photos_pixel ON photos(pixel_sha);
CREATE TABLE IF NOT EXISTS pairs (
    a TEXT NOT NULL, b TEXT NOT NULL,          -- sha256, a < b
    verdict TEXT NOT NULL,                      -- exact | copy | similar
    reason TEXT, sscd REAL, pdq INTEGER, orb_inliers INTEGER,
    found_at TEXT NOT NULL,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS reviews (            -- a person's verdict on a pair, outlives the 7-day window
    a TEXT NOT NULL, b TEXT NOT NULL,           -- sha256, a < b
    verdict TEXT NOT NULL,                      -- not_dup
    at TEXT NOT NULL,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS files (              -- path -> sha256, so a re-scan of a big folder skips re-reading
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series (             -- "two frames of a series?" per pair: needs both files, so it is cached
    a TEXT NOT NULL, b TEXT NOT NULL, ver TEXT NOT NULL, series INTEGER, share REAL, inliers INTEGER,
    PRIMARY KEY (a, b, ver)
);
CREATE TABLE IF NOT EXISTS choices (            -- "keep" or "remove" a person picked for a photo in one checked folder or batch
    scope TEXT NOT NULL, sha256 TEXT NOT NULL, choice TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (scope, sha256)
);
CREATE TABLE IF NOT EXISTS moves (              -- files moved to a duplicates folder; undo moves them back
    id INTEGER PRIMARY KEY, move_id TEXT NOT NULL, root TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
    sha256 TEXT, at TEXT NOT NULL, undone_at TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def known(self, sha: str) -> bool:
        r = self.db.execute("SELECT feature_ver FROM photos WHERE sha256=?", (sha,)).fetchone()
        return r is not None and r[0] == FEATURE_VERSION

    def put(self, fp: Fingerprint, batch_id: str, now: datetime):
        self.db.execute(
            "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fp.sha256, fp.path, fp.size_bytes, fp.width, fp.height, fp.pixel_sha, fp.pdq.tobytes(),
             fp.pdq_quality, fp.sscd.tobytes(), fp.orb_xy.tobytes(), fp.orb_des.tobytes(), fp.captured_at,
             fp.camera, batch_id, now.isoformat(), FEATURE_VERSION))

    def put_pair(self, a: str, b: str, verdict: str, reason: str, sscd: float, pdq: int, inl: int, now: datetime):
        a, b = sorted((a, b))
        self.db.execute("INSERT OR REPLACE INTO pairs VALUES (?,?,?,?,?,?,?,?)",
                        (a, b, verdict, reason, sscd, pdq, inl, now.isoformat()))

    def cached_sha(self, path: str, size: int, mtime: float) -> str | None:
        r = self.db.execute("SELECT sha256 FROM files WHERE path=? AND size=? AND mtime=?", (path, size, mtime)).fetchone()
        return r[0] if r else None

    def put_file(self, path: str, size: int, mtime: float, sha: str):
        self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (path, size, mtime, sha))

    def reviews(self) -> dict[tuple[str, str], str]:
        return {(a, b): v for a, b, v in self.db.execute("SELECT a, b, verdict FROM reviews")}

    def set_review(self, a: str, b: str, verdict: str | None, now: datetime):
        a, b = sorted((a, b))
        if verdict:
            self.db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?)", (a, b, verdict, now.isoformat()))
        else:
            self.db.execute("DELETE FROM reviews WHERE a=? AND b=?", (a, b))

    def get_series(self, a: str, b: str, ver: str):
        a, b = sorted((a, b))
        r = self.db.execute("SELECT series, share, inliers FROM series WHERE a=? AND b=? AND ver=?", (a, b, ver)).fetchone()
        return None if r is None else dict(series=None if r[0] is None else bool(r[0]), code=r[0], share=r[1], inliers=r[2])

    def put_series(self, a: str, b: str, ver: str, res: dict):
        a, b = sorted((a, b))
        s = res.get("code", res.get("series"))  # a plain yes/no, or a small code (re-photo check: 1 re-shot, 2 crop)
        self.db.execute("INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?)",
                        (a, b, ver, None if s is None else int(s), res.get("share"), res.get("inliers")))

    def choices(self, scope: str) -> dict[str, str]:
        return {s: c for s, c in self.db.execute("SELECT sha256, choice FROM choices WHERE scope=?", (scope,))}

    def set_choice(self, scope: str, sha: str, choice: str | None, now: datetime):
        if choice in ("keep", "remove"):
            self.db.execute("INSERT OR REPLACE INTO choices VALUES (?,?,?,?)", (scope, sha, choice, now.isoformat()))
        else:
            self.db.execute("DELETE FROM choices WHERE scope=? AND sha256=?", (scope, sha))

    def commit(self):
        self.db.commit()

    def window(self, now: datetime, days: int = 7) -> list[dict]:
        since = (now - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT sha256, path, width, height, pixel_sha, pdq, pdq_quality, sscd, orb_xy, orb_des, captured_at,"
            " camera, batch_id, ingested_at, size_bytes FROM photos WHERE ingested_at >= ? AND ingested_at <= ? AND feature_ver=?",
            (since, now.isoformat(), FEATURE_VERSION)).fetchall()
        out = []
        for r in rows:
            out.append(dict(sha256=r[0], path=r[1], width=r[2], height=r[3], pixel_sha=r[4],
                            pdq=np.frombuffer(r[5], np.uint8).reshape(8, 32), pdq_quality=r[6],
                            sscd=np.frombuffer(r[7], np.float16), orb_xy=np.frombuffer(r[8], np.float32).reshape(-1, 2),
                            orb_des=np.frombuffer(r[9], np.uint8).reshape(-1, 32), captured_at=r[10], camera=r[11],
                            batch_id=r[12], ingested_at=r[13], size_bytes=r[14]))
        return out

    def purge(self, now: datetime, days: int = 7) -> int:
        since = (now - timedelta(days=days)).isoformat()
        n = self.db.execute("DELETE FROM photos WHERE ingested_at < ?", (since,)).rowcount
        self.db.execute("DELETE FROM pairs WHERE found_at < ?", (since,))
        return n
