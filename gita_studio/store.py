"""SQLite persistence: ideas, posts (with their full package), metrics and strategy briefs."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    chapter       INTEGER NOT NULL,
    verse         INTEGER NOT NULL,
    format        TEXT NOT NULL,
    angle         TEXT NOT NULL,
    status        TEXT NOT NULL,         -- needs_review | approved | rejected | scheduled | published | failed
    fidelity      INTEGER,
    package       TEXT NOT NULL,         -- JSON: idea, script, review, visuals, distribution
    scheduled_at  TEXT,
    published_at  TEXT,
    publish_result TEXT,
    note          TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    post_id     INTEGER NOT NULL REFERENCES posts(id),
    platform    TEXT NOT NULL,
    views       INTEGER DEFAULT 0,
    likes       INTEGER DEFAULT 0,
    comments    INTEGER DEFAULT 0,
    shares      INTEGER DEFAULT 0,
    saves       INTEGER DEFAULT 0,
    follows     INTEGER DEFAULT 0,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (post_id, platform)
);
CREATE TABLE IF NOT EXISTS strategy (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    brief      TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # posts
    def add_post(self, *, chapter: int, verse: int, format: str, angle: str, status: str,
                 fidelity: int | None, package: dict[str, Any], note: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO posts (created_at, chapter, verse, format, angle, status, fidelity, package, note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now(), chapter, verse, format, angle, status, fidelity, json.dumps(package, ensure_ascii=False), note),
        )
        self.db.commit()
        return cur.lastrowid

    def get_post(self, post_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()

    def posts(self, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if status:
            return self.db.execute(
                "SELECT * FROM posts WHERE status = ? ORDER BY id LIMIT ?", (status, limit)).fetchall()
        return self.db.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def set_status(self, post_id: int, status: str, **fields: Any) -> None:
        allowed = {"scheduled_at", "published_at", "publish_result", "note"}
        sets, args = ["status = ?"], [status]
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            sets.append(f"{k} = ?")
            args.append(v)
        self.db.execute(f"UPDATE posts SET {', '.join(sets)} WHERE id = ?", (*args, post_id))
        self.db.commit()

    def used_verses(self, days: int = 60) -> list[tuple[int, int]]:
        rows = self.db.execute(
            "SELECT chapter, verse FROM posts WHERE status != 'rejected' "
            "AND created_at >= datetime('now', ?)", (f"-{days} days",)).fetchall()
        return [(r["chapter"], r["verse"]) for r in rows]

    def taken_slots(self) -> set[str]:
        rows = self.db.execute("SELECT scheduled_at FROM posts WHERE scheduled_at IS NOT NULL "
                               "AND status IN ('scheduled', 'published')").fetchall()
        return {r["scheduled_at"] for r in rows}

    def due_posts(self, at: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at",
            (at,)).fetchall()

    # metrics
    def upsert_metrics(self, post_id: int, platform: str, **m: int) -> None:
        cols = ["views", "likes", "comments", "shares", "saves", "follows"]
        vals = [int(m.get(c, 0) or 0) for c in cols]
        self.db.execute(
            f"INSERT OR REPLACE INTO metrics (post_id, platform, {', '.join(cols)}, recorded_at)"
            f" VALUES (?, ?, {', '.join('?' * len(cols))}, ?)", (post_id, platform, *vals, now()))
        self.db.commit()

    def performance(self, limit: int = 60) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT p.id, p.chapter, p.verse, p.format, p.angle,
                      json_extract(p.package, '$.script.hook') AS hook,
                      SUM(m.views) views, SUM(m.likes) likes, SUM(m.comments) comments,
                      SUM(m.shares) shares, SUM(m.saves) saves, SUM(m.follows) follows
               FROM posts p JOIN metrics m ON m.post_id = p.id
               GROUP BY p.id ORDER BY p.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # strategy
    def save_strategy(self, brief: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO strategy (created_at, brief) VALUES (?, ?)", (now(), json.dumps(brief)))
        self.db.commit()

    def latest_strategy(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT brief FROM strategy ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["brief"]) if row else None
