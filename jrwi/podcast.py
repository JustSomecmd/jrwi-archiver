"""The members' private podcast feed (RSS), which the site offers for podcast apps.

It only lists audio posts, so it adds nothing to the archive's content. What it does give:
- the time of day each episode came out, where the site only shows the date (kept as posts.published_at);
- a quick sign that something new is up: reading it is one small request, without logging in, so it's read every
  few minutes and a new episode in it starts a check of the site, where the same post has most likely gone up too.

The feed's address carries a private key, so it's kept in the settings like a password: never shown again, never
logged, and errors name only the host. Nothing from it that could hold the key (the audio's address) is stored.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from defusedxml import ElementTree as ET

from . import organize
from .db import Db, now
from .site.client import USER_AGENT

log = logging.getLogger("jrwi.podcast")

MAX_BYTES = 30 * 1024 * 1024
NEW_FOR = timedelta(hours=6)        # how long a new episode the archive hasn't found keeps starting checks
TRY_EVERY = timedelta(minutes=30)
MAX_TRIES = 3
RECENT = timedelta(days=7)          # older episodes turning up in the feed don't start checks
TITLE_DAYS = 2                      # a post matched by its title must be from within this many days
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "content": "http://purl.org/rss/1.0/modules/content/",
      "podcast": "https://podcastindex.org/namespace/1.0", "media": "http://search.yahoo.com/mrss/",
      "atom": "http://www.w3.org/2005/Atom", "googleplay": "http://www.google.com/schemas/play-podcasts/1.0"}
PREFIX = {v: k for k, v in NS.items()}
_lock = threading.Lock()


class FeedError(Exception):
    pass


def host_of(url: str) -> str:
    return urlparse(url or "").hostname or "the feed"


def tag_name(el) -> str:
    """An element's name as the feed writes it, such as itunes:duration."""
    t = el.tag
    if t.startswith("{"):
        uri, local = t[1:].split("}", 1)
        return f"{PREFIX.get(uri, 'ns')}:{local}"
    return t


def text_of(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def seconds(value: str) -> int | None:
    """itunes:duration as HH:MM:SS, MM:SS or seconds."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parts = [float(p) for p in value.split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return int(total)


def when(value: str) -> tuple[str | None, bool]:
    """An RSS date (RFC 822) in UTC, and whether it gives a time of day."""
    value = (value or "").strip()
    if not value:
        return None, False
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None, False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    has_time = bool(re.search(r"\d{1,2}:\d{2}", value)) and (d.hour, d.minute, d.second) != (0, 0, 0)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds"), has_time


def plain(html: str, limit: int = 4000) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def int_or_none(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def parse(data: bytes) -> dict:
    """The channel and its items. Never keeps the audio's address, which can carry the feed's key."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"not a feed the archiver can read ({e})") from None
    channel = root.find("channel")
    if channel is None:
        raise FeedError("not an RSS feed (no channel)")
    items = []
    for it in channel.findall("item"):
        fields = sorted({tag_name(c) for c in it})
        enc = it.find("enclosure")
        published, has_time = when(text_of(it.find("pubDate")))
        guid = text_of(it.find("guid")) or text_of(it.find("link")) or text_of(it.find("title"))
        if not guid:
            continue
        length = (enc.get("length") or "") if enc is not None else ""
        items.append({
            "guid": guid[:500], "title": text_of(it.find("title"))[:500], "link": text_of(it.find("link"))[:1000],
            "published": published, "has_time": int(has_time),
            "duration": seconds(text_of(it.find("itunes:duration", NS))),
            "enclosure_type": enc.get("type") if enc is not None else None,
            "enclosure_length": int(length) if length.isdigit() else None,
            "enclosure_host": host_of(enc.get("url") or "") if enc is not None else None,
            "description": plain(text_of(it.find("content:encoded", NS)) or text_of(it.find("description"))),
            "season": int_or_none(text_of(it.find("itunes:season", NS))),
            "episode": int_or_none(text_of(it.find("itunes:episode", NS))),
            "feed_fields": fields,
        })
    return {"title": text_of(channel.find("title")), "items": items,
            "channel_fields": sorted({tag_name(c) for c in channel if c.tag != "item"})}


def fetch(url: str, state: dict) -> tuple[int, bytes | None, dict]:
    """GET the feed, sending what it said last time so an unchanged feed answers 304 with nothing."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    try:
        with requests.get(url, headers=headers, timeout=(15, 60), stream=True) as r:
            if r.status_code == 304:
                return 304, None, {}
            if r.status_code >= 400:
                raise FeedError(f"{host_of(url)} answered HTTP {r.status_code}")
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise FeedError("the feed is larger than 30 MB")
            return r.status_code, data, {"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified")}
    except requests.RequestException as e:
        # The exception's text holds the address, key included: say only what went wrong and where
        raise FeedError(f"couldn't reach {host_of(url)} ({type(e).__name__})") from None


def state(db: Db) -> dict:
    return db.setting("feed_state") or {}


def poll(db: Db, force: bool = False) -> dict:
    """Read the feed, keep its items, match them to posts, and say whether a check should start: when an episode
    the archive hasn't found is new in the feed (not on the first read, and not an old episode turning up)."""
    url = (db.setting("podcast_feed") or "").strip()
    if not url:
        return {"ok": False, "error": "no feed address in Settings", "start_check": False}
    with _lock:
        st = state(db)
        st["last_read"] = now()
        try:
            code, data, meta = fetch(url, {} if force else st)
            if code != 304:
                feed = parse(data)
        except FeedError as e:
            st.update(ok=False, error=str(e))
            db.set_setting("feed_state", st)
            log.info("podcast feed: %s", e)
            return {"ok": False, "error": str(e), "start_check": False}
        st.update(ok=True, error=None)
        new: list[str] = []
        if code != 304:
            st.update(etag=meta.get("etag"), last_modified=meta.get("last_modified"), title=feed["title"],
                      items=len(feed["items"]), channel_fields=feed["channel_fields"], last_changed=st["last_read"])
            new = store(db, feed["items"], st["last_read"])
        matched = match(db)
        due = due_items(db)
        stamp = now()
        for g in due:
            db.x("UPDATE feed_items SET tries = tries + 1, last_try = ? WHERE guid = ?", (stamp, g["guid"]))
        if due:
            st["last_trigger"] = {"at": stamp, "titles": [g["title"] for g in due][:5]}
        db.set_setting("feed_state", st)
        return {"ok": True, "changed": code != 304, "new": new, "matched": matched,
                "start_check": bool(due), "due": [g["title"] for g in due]}


def store(db: Db, items: list[dict], seen_at: str) -> list[str]:
    """Keep the feed's items. Items there on the very first read are the baseline, which never starts checks."""
    first = db.one("SELECT 1 FROM feed_items LIMIT 1") is None
    have = {r["guid"] for r in db.q("SELECT guid FROM feed_items")}
    new = []
    for it in items:
        if it["guid"] in have:
            db.update("feed_items", "guid", it["guid"], {**it, "gone": None})
        else:
            db.upsert("feed_items", "guid", {**it, "first_seen": seen_at, "baseline": int(first), "tries": 0})
            new.append(it["title"])
    # Items the feed no longer lists stay, marked
    listed = {it["guid"] for it in items}
    for g in have - listed:
        db.x("UPDATE feed_items SET gone = COALESCE(gone, ?) WHERE guid = ?", (seen_at, g))
    return new


def due_items(db: Db) -> list[dict]:
    t = datetime.now(timezone.utc)
    out = []
    for r in db.q("SELECT * FROM feed_items WHERE post_id IS NULL AND NOT baseline AND gone IS NULL AND tries < ?",
                  (MAX_TRIES,)):
        if utc(r["first_seen"]) < t - NEW_FOR:
            continue
        if r["published"] and utc(r["published"]) < t - RECENT:
            continue
        if r["last_try"] and utc(r["last_try"]) > t - TRY_EVERY:
            continue
        out.append(r)
    return out


def utc(value: str) -> datetime:
    """A stored date or time; the site's dates have no time zone, and count as UTC like everywhere else."""
    d = datetime.fromisoformat(value)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _path(url: str) -> str:
    return urlparse(url or "").path.rstrip("/")


def match(db: Db) -> int:
    """Link the feed's items to the archive's posts: by the post's address, then by a post ID in the item's guid
    or link that has the same title, then by the same title (without "AUDIO ONLY" and the like) within two days,
    audio posts first. A matched post takes the item's time of day (published_at). Returns how many it linked."""
    items = db.q("SELECT * FROM feed_items WHERE post_id IS NULL")
    if not items:
        return 0
    posts = db.q("SELECT id, url, title, published, path_kind, media FROM posts WHERE status != 'locked'")
    by_path = {_path(p["url"]): p for p in posts if p["url"]}
    by_id = {p["id"]: p for p in posts}
    by_title: dict[str, list[dict]] = {}
    by_base: dict[str, list[dict]] = {}
    for p in posts:
        by_title.setdefault(organize.norm(p["title"]), []).append(p)
        by_base.setdefault(organize.base_title(p["title"]), []).append(p)

    def is_audio(p):
        return organize.is_audio_post(p) or bool((p.get("media") or {}).get("audio"))

    def near(p, it) -> float:
        if not (p["published"] and it["published"]):
            return 0.0
        return abs((utc(p["published"]) - utc(it["published"])).total_seconds())

    def pick(cands, it):
        cands = [p for p in cands if near(p, it) <= TITLE_DAYS * 86400]
        return min(cands, key=lambda p: (not is_audio(p), near(p, it), -p["id"])) if cands else None

    linked = 0
    for it in items:
        post = by_path.get(_path(it["link"])) if it["link"] and urlparse(it["link"]).path.startswith("/supporters/") else None
        if post is None:
            for n in re.findall(r"\b\d{4,9}\b", f"{it['guid']} {it['link']}"):
                p = by_id.get(int(n))
                if p and organize.base_title(p["title"]) == organize.base_title(it["title"]):
                    post = p
                    break
        if post is None:
            post = pick(by_title.get(organize.norm(it["title"]), []), it) or \
                pick(by_base.get(organize.base_title(it["title"]), []), it)
        if post is None:
            continue
        db.x("UPDATE feed_items SET post_id = ? WHERE guid = ?", (post["id"], it["guid"]))
        if it["has_time"] and it["published"]:
            db.x("UPDATE posts SET published_at = ? WHERE id = ?", (it["published"], post["id"]))
        linked += 1
    return linked


def summary(db: Db) -> dict:
    """What the admin page shows: when it was read, what the feed holds, and how it lines up with the archive."""
    rows = db.q("SELECT * FROM feed_items ORDER BY published DESC, first_seen DESC")
    listed = [r for r in rows if not r["gone"]]
    fields: dict[str, int] = {}
    for r in listed:
        for f in r["feed_fields"] or []:
            fields[f] = fields.get(f, 0) + 1
    return {"state": state(db), "count": len(listed), "gone": len(rows) - len(listed),
            "matched": sum(1 for r in listed if r["post_id"]),
            "with_time": sum(1 for r in listed if r["has_time"]),
            "times_used": db.one("SELECT COUNT(*) AS n FROM posts WHERE published_at IS NOT NULL")["n"],
            "fields": sorted(fields.items(), key=lambda kv: (-kv[1], kv[0])),
            "unmatched": [r for r in listed if not r["post_id"]][:20],
            "latest": listed[:10]}
