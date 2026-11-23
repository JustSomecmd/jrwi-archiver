"""SQLite storage. One connection shared between the web server and the checker, behind a lock."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DEFAULTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    url TEXT, path_kind TEXT, title TEXT,
    published TEXT, date_text TEXT,
    access TEXT, locked INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0,
    tag_ids TEXT DEFAULT '[]', duration INTEGER,
    like_count INTEGER, comment_count INTEGER,
    body_html TEXT, body_text TEXT,
    links TEXT DEFAULT '[]', images TEXT DEFAULT '[]', attachments TEXT DEFAULT '[]',
    documents TEXT DEFAULT '[]', embeds TEXT DEFAULT '[]', media TEXT DEFAULT '{}',
    thumbnail TEXT, files TEXT DEFAULT '{}',
    show_id TEXT, tab TEXT, episode INTEGER, special INTEGER, pair_id INTEGER,
    show_override TEXT, tab_override TEXT, episode_override INTEGER, season INTEGER, season_override INTEGER,
    tabs TEXT DEFAULT '[]', places TEXT DEFAULT '[]', handover TEXT, site_gone TEXT, episode_sort REAL, published_at TEXT,
    status TEXT DEFAULT 'new', problems TEXT DEFAULT '[]', skipped TEXT DEFAULT '[]',
    first_seen TEXT, last_fetched TEXT, comments_fetched TEXT, refetch TEXT, sync TEXT
);
CREATE INDEX IF NOT EXISTS posts_show ON posts(show_id, tab);
CREATE INDEX IF NOT EXISTS posts_published ON posts(published);
-- The other versions of a post are looked up for every post a list shows
CREATE INDEX IF NOT EXISTS posts_pair ON posts(pair_id);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY, post_id INTEGER, parent_id TEXT,
    author_name TEXT, author_avatar TEXT, date TEXT, date_text TEXT,
    body_html TEXT, body_text TEXT, like_count INTEGER, position INTEGER,
    raw TEXT, pinned INTEGER DEFAULT 0, link TEXT, author_badges TEXT DEFAULT '[]', author_color TEXT,
    author_id TEXT
);
CREATE INDEX IF NOT EXISTS comments_post ON comments(post_id, position);

CREATE TABLE IF NOT EXISTS shows (
    id TEXT PRIMARY KEY, name TEXT, description TEXT, page_url TEXT,
    poster TEXT, banner TEXT, sort INTEGER, aliases TEXT DEFAULT '[]',
    source TEXT, art TEXT, art_source TEXT, art_options TEXT DEFAULT '[]', seasons TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS tags (id TEXT PRIMARY KEY, name TEXT, count INTEGER);

CREATE TABLE IF NOT EXISTS pages (
    path TEXT PRIMARY KEY, title TEXT, file TEXT, fetched TEXT, main_html TEXT
);

CREATE TABLE IF NOT EXISTS progress (
    post_id INTEGER PRIMARY KEY, position REAL, duration REAL, finished INTEGER, updated_at TEXT, timeline TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, at TEXT, post_id INTEGER, title TEXT, message TEXT
);

-- The members' private podcast feed: its items, and the post each is (podcast.py)
CREATE TABLE IF NOT EXISTS feed_items (
    guid TEXT PRIMARY KEY, title TEXT, link TEXT, published TEXT, has_time INTEGER DEFAULT 0, duration INTEGER,
    enclosure_type TEXT, enclosure_length INTEGER, enclosure_host TEXT, description TEXT, season INTEGER,
    episode INTEGER, feed_fields TEXT DEFAULT '[]', first_seen TEXT, baseline INTEGER DEFAULT 0, post_id INTEGER,
    tries INTEGER DEFAULT 0, last_try TEXT, gone TEXT
);

-- Timed transcripts (contract v0.23): one per item, its lines in archive/transcripts/<item_id>.json.gz
CREATE TABLE IF NOT EXISTS transcripts (
    item_id INTEGER PRIMARY KEY, document TEXT, doc_size INTEGER, lines INTEGER, aligned INTEGER DEFAULT 0,
    timing TEXT, updated_at TEXT
);

-- Every time an episode's transcript was timed (0.13.4), so earlier tries aren't lost when it's timed again
CREATE TABLE IF NOT EXISTS transcript_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, title TEXT, at TEXT, timing TEXT
);
CREATE INDEX IF NOT EXISTS transcript_runs_item ON transcript_runs(item_id, at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, result TEXT, message TEXT,
    new_items INTEGER DEFAULT 0, archived INTEGER DEFAULT 0
);
"""

JSON_FIELDS = {"tag_ids", "links", "images", "attachments", "documents", "embeds", "media", "files",
               "problems", "skipped", "aliases", "author_badges", "sync", "tabs", "art_options", "places", "seasons",
               "handover", "feed_fields", "timing"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Db:
    def __init__(self, path: Path) -> None:
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # Safe with WAL: a power cut can lose the last moments' writes, never the database
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=10000")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns that newer versions use to databases made by older ones."""
        wanted = {"posts": {"refetch": "TEXT", "sync": "TEXT", "season": "INTEGER", "season_override": "INTEGER",
                            "tabs": "TEXT DEFAULT '[]'", "places": "TEXT DEFAULT '[]'",
                            "handover": "TEXT", "site_gone": "TEXT",
                            "episode_sort": "REAL", "published_at": "TEXT"},
                  "shows": {"source": "TEXT", "art": "TEXT", "art_source": "TEXT", "art_options": "TEXT DEFAULT '[]'",
                            "seasons": "TEXT DEFAULT '[]'"},
                  "progress": {"timeline": "TEXT"},
                  "comments": {"pinned": "INTEGER DEFAULT 0", "link": "TEXT", "author_badges": "TEXT DEFAULT '[]'", "author_color": "TEXT",
                               "author_id": "TEXT"}}
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, decl in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    if (table, col) == ("shows", "art"):
                        # 0.6.0: a show's picture is kept as it is until another explicit one turns up
                        self.conn.execute("UPDATE shows SET art = COALESCE(banner, poster), art_source = 'kept' "
                                          "WHERE COALESCE(banner, poster) IS NOT NULL")
        # 0.8.0: a picture picked on the admin page became an override, which stays until it's turned off
        self.conn.execute("UPDATE shows SET art_source = 'override' WHERE art_source = 'picked'")
        # 0.13.4: the record of every timing starts with what each episode's last one left
        if not self.conn.execute("SELECT 1 FROM transcript_runs LIMIT 1").fetchone():
            for row in self.conn.execute("SELECT t.item_id, t.timing, p.title FROM transcripts t "
                                         "LEFT JOIN posts p ON p.id = t.item_id").fetchall():
                try:
                    timing = json.loads(row["timing"] or "{}")
                except ValueError:
                    continue
                if timing.get("at"):
                    self.conn.execute("INSERT INTO transcript_runs (item_id, title, at, timing) VALUES (?, ?, ?, ?)",
                                      (row["item_id"], row["title"], timing["at"], row["timing"]))

    def drop_comment_raw(self) -> None:
        """Before 0.10.3 each comment's HTML was kept in the database too (up to 200 KB each), though nothing read
        it: the comment pages are saved as files. Empty that column once and give the space back."""
        if self.setting("comments_raw_dropped"):
            return
        with self.lock:
            had = self.conn.execute("SELECT COUNT(*) FROM comments WHERE raw IS NOT NULL").fetchone()[0]
            if had:
                self.conn.execute("UPDATE comments SET raw = NULL WHERE raw IS NOT NULL")
                self.conn.execute("VACUUM")
        self.set_setting("comments_raw_dropped", True)

    # ------------------------------------------------------------------ basics

    def q(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(sql, tuple(args)).fetchall()
        return [self._decode(r) for r in rows]

    def one(self, sql: str, args: Iterable = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: Iterable = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, tuple(args))
            return cur.lastrowid

    def close_unfinished_runs(self, message: str) -> int:
        """Record checks that have no end as stopped, with a message saying why. A check's row is written when
        it starts and finished when it ends, so one cut short by the archiver stopping would say running for good."""
        with self.lock:
            return self.conn.execute("UPDATE runs SET finished=?, result='stopped', message=? WHERE finished IS NULL",
                                     (now(), message)).rowcount

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in JSON_FIELDS & d.keys():
            if isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    @staticmethod
    def _encode(v: Any) -> Any:
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool):
            return int(v)
        return v

    def upsert(self, table: str, key: str, row: dict) -> None:
        cols = list(row.keys())
        vals = [self._encode(row[c]) for c in cols]
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key)
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
               f"ON CONFLICT({key}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING"))
        self.x(sql, vals)

    def update(self, table: str, key: str, key_value: Any, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{c}=?" for c in fields)
        self.x(f"UPDATE {table} SET {sets} WHERE {key}=?", [self._encode(v) for v in fields.values()] + [key_value])

    # ------------------------------------------------------------------ settings

    def setting(self, key: str) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return DEFAULTS.get(key)
        try:
            return json.loads(row["value"])
        except ValueError:
            return row["value"]

    def settings(self) -> dict:
        out = dict(DEFAULTS)
        for r in self.q("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                out[r["key"]] = r["value"]
        return out

    def set_setting(self, key: str, value: Any) -> None:
        self.x("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # ------------------------------------------------------------------ posts

    def post(self, post_id: int) -> dict | None:
        return self.one("SELECT * FROM posts WHERE id=?", (post_id,))

    def event(self, type_: str, message: str = "", post_id: int | None = None, title: str | None = None) -> None:
        self.x("INSERT INTO events (type, at, post_id, title, message) VALUES (?, ?, ?, ?, ?)",
               (type_, now(), post_id, title, message))
