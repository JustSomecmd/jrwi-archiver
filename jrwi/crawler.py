"""One check of the members site: read the feed, archive new and changed posts, refresh comments."""

from __future__ import annotations

import base64
import gzip
import json
import time
import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests

from . import organize
from .config import DRIVE_DOWNLOAD, GDOCS_BASE, MUX_BASE, SITE_BASE, Paths
from .db import Db, now
from .site import parse as P
from .site.client import (HTML_ACCEPT, TURBO_ACCEPT, Blocked, Client, LoginFailed, NotFound, SessionExpired,
                          SiteError, logged_in)
from .site.download import Downloader, DownloadError, glob_escape, probe_height
from .sync import find_offset, probe_duration

log = logging.getLogger("jrwi.crawler")

PAGES_EVERY = timedelta(hours=24)
STOP_AFTER_KNOWN = 15


@dataclass
class Status:
    running: bool = False
    phase: str = "idle"
    detail: str = ""
    done: int = 0
    total: int = 0
    current: float | None = None   # how much of the file downloading now is done, 0 to 1
    started: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "phase": self.phase, "detail": self.detail,
                    "done": self.done, "total": self.total, "current": self.current, "started": self.started}


def write_gz(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)


def read_gz(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


def asset_name(url: str) -> str:
    ext = P.ext_of(url)
    if not ext or len(ext) > 6:
        ext = ".bin"
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ext


class Archiver:
    def __init__(self, db: Db, paths: Paths, status: Status | None = None) -> None:
        self.db = db
        self.paths = paths
        self.status = status or Status()
        self.stop_event = threading.Event()
        self.client = Client(SITE_BASE, paths.data / "cookies.json")
        self.client.should_stop = self.stop_event.is_set
        self.new_titles: list[str] = []

    # ------------------------------------------------------------------ helpers

    @property
    def s(self) -> dict:
        return self.db.settings()

    def post_dir(self, post_id: int) -> Path:
        d = self.paths.archive / "posts" / str(post_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def rel(self, path: Path) -> str:
        return path.relative_to(self.paths.archive).as_posix()

    def same_site(self, url: str) -> bool:
        return self.client.same_site(url)

    def ensure_login(self, soup=None) -> None:
        if soup is not None and logged_in(soup):
            return
        s = self.s
        email = os.environ.get("JRWI_EMAIL") or s["site_email"]
        password = os.environ.get("JRWI_PASSWORD") or s["site_password"]
        before = self.status.snapshot()
        self.status.set(phase="logging in", detail="")
        self.client.login(email, password, debug_file=self.paths.data / "login-debug.txt")
        self.status.set(phase=before["phase"], detail=before["detail"])

    def has_login(self) -> bool:
        """True when there's an email and password, or a saved login session, to check with."""
        s = self.s
        email = os.environ.get("JRWI_EMAIL") or s["site_email"]
        password = os.environ.get("JRWI_PASSWORD") or s["site_password"]
        return bool(email and password) or self.client.cookie_file.exists()

    def page(self, url: str, accept: str = HTML_ACCEPT, headers: dict | None = None) -> tuple[str, str]:
        """GET a page, logging in again once if the session ran out."""
        try:
            return self.client.page(url, accept=accept, headers=headers)
        except SessionExpired:
            self.ensure_login()
            return self.client.page(url, accept=accept, headers=headers)

    # ------------------------------------------------------------------ the check

    def run_check(self, full: bool = False) -> dict:
        s = self.s
        self.client.delay = float(s["request_delay"])
        self.new_titles = []
        self.stop_event.clear()
        run_id = self.db.x("INSERT INTO runs (started, result) VALUES (?, 'running')", (now(),))
        self.status.set(running=True, phase="starting", detail="", done=0, total=0, started=now())
        result, message, archived = "ok", None, 0
        try:
            organize.ensure_default_shows(self.db)
            feed_soup = self.crawl_feed(full=full)
            # Site pages first: a few pages (skipped when saved recently) that give the logo and the show art, so
            # those show up straight away instead of after a long archive
            if s["public_pages"]:
                self.archive_pages(feed_soup)
            organize.drop_shared_show_art(self.db)
            organize.classify_all(self.db)
            archived = self.archive_posts()
            if s["comments"]:
                self.refresh_comments()
            self.line_up()
            organize.classify_all(self.db)
            organize.reorganize(self.db, self.paths)
            organize.refresh_jellyfin(self.db, self.paths)
        except LoginFailed as e:
            result, message = "login_failed", str(e)
        except Blocked as e:
            result, message = "blocked", str(e)
        except SiteError as e:
            result, message = ("stopped", "stopped by request") if self.stop_event.is_set() else ("error", str(e))
        except Exception as e:  # keep the service alive and report it
            log.exception("check failed")
            result, message = "error", f"{type(e).__name__}: {e}"
        finally:
            self.status.set(running=False, phase="idle", detail="", done=0, total=0, current=None)
        try:
            self.db.set_setting("archive_bytes", folder_size(self.paths.archive) + folder_size(self.paths.media))
        except OSError:
            pass
        self.db.update("runs", "id", run_id, {"finished": now(), "result": result, "message": message,
                                               "new_items": len(self.new_titles), "archived": archived})
        if result == "login_failed":
            self.db.event("login_failed", message or "")
        elif result in ("blocked", "error"):
            self.db.event("check_failed", message or "")
        self.notify(result, message)
        return {"result": result, "message": message, "new_items": len(self.new_titles), "archived": archived}

    def notify(self, result: str, message: str | None) -> None:
        url = (self.s.get("notify_url") or "").strip()
        if not url:
            return
        lines = []
        if self.new_titles:
            lines.append("New on Just Roll With It:\n" + "\n".join(f"- {t}" for t in self.new_titles[:15]))
        if result in ("login_failed", "blocked", "error"):
            lines.append(f"JRWI archiver problem ({result}): {message}")
        if not lines:
            return
        text = "\n\n".join(lines)
        try:
            if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
                requests.post(url, json={"content": text[:1900]}, timeout=20)
            else:
                requests.post(url, data=text.encode(), headers={"Title": "JRWI archiver"}, timeout=20)
        except requests.RequestException as e:
            log.warning("notification failed: %s", e)

    # ------------------------------------------------------------------ feed

    def crawl_feed(self, full: bool = False):
        incremental = bool(self.s.get("crawl_complete")) and not full
        self.status.set(phase="reading the feed", detail="")
        url: str | None = self.client.abs("/supporters")
        visited, seen_ids = set(), set()
        known_streak = page_no = 0
        reached_end = False
        first_soup = None
        while url:
            if url in visited:
                break
            visited.add(url)
            page_no += 1
            final, text = self.page(url, accept=HTML_ACCEPT if page_no == 1 else TURBO_ACCEPT)
            soup = P.soup_of(text, turbo=page_no > 1)
            if page_no == 1:
                if not logged_in(soup):
                    self.ensure_login()
                    final, text = self.client.page(url)
                    soup = P.soup_of(text)
                    if not logged_in(soup):
                        raise LoginFailed("logged in, but the members feed still shows the login button")
                first_soup = soup
                write_gz(self.paths.archive / "site" / "feed.html.gz", text)
                self.save_tags(P.parse_tags(soup))
                organize.merge_show_menu(self.db, P.parse_show_menu(soup, final))
            frames = P.find_post_frames(soup)
            for frame in frames:
                card = P.parse_card(frame, final)
                if not card or card["id"] in seen_ids:
                    continue
                seen_ids.add(card["id"])
                pills = card.pop("tag_pills", [])
                for t in pills:
                    self.db.upsert("tags", "id", {"id": str(t["id"]), "name": t["name"]})
                if pills:
                    card["tag_ids"] = [str(t["id"]) for t in pills]
                if not card["locked"]:
                    write_gz(self.post_dir(card["id"]) / "card.html.gz", P.clean_card(frame))
                if self.merge_card(card):
                    known_streak = 0
                elif not card["pinned"]:
                    known_streak += 1
            self.status.set(detail=f"page {page_no}, {len(seen_ids)} posts")
            if incremental and known_streak >= STOP_AFTER_KNOWN:
                break
            nxt = P.find_load_more(soup, final)
            if not frames or not nxt:
                reached_end = True
                break
            url = nxt
        if reached_end:
            self.db.set_setting("crawl_complete", True)
        return first_soup

    def save_tags(self, tags: list[dict]) -> None:
        for t in tags:
            self.db.upsert("tags", "id", {"id": str(t["id"]), "name": t["name"], "count": t["count"]})

    def merge_card(self, card: dict) -> bool:
        """Store what the feed shows about a post. Returns True if the post is new."""
        old = self.db.post(card["id"])
        row = {k: v for k, v in card.items() if v is not None}
        if old is None:
            row.update(status="locked" if card["locked"] else "new", first_seen=now())
            self.db.upsert("posts", "id", row)
            return True
        if card["title"].startswith("Post ") and old.get("title"):
            row.pop("title", None)
        if old["status"] == "locked" and not card["locked"]:
            row["status"] = "new"
        elif card["locked"] and old["status"] not in ("complete",):
            row["status"] = "locked"
        for k in ("published",):
            if old.get(k):
                row.pop(k, None)
        self.db.update("posts", "id", card["id"], {k: v for k, v in row.items() if k != "id"})
        return False

    # ------------------------------------------------------------------ posts

    def wanted(self) -> dict[str, bool]:
        s = self.s
        return {"video": s["video"], "audio": s["audio"], "images": s["images"], "attachments": s["attachments"],
                "gdocs": s["google_docs"], "embeds": s["youtube_embeds"], "comments": s["comments"]}

    def archive_posts(self) -> int:
        wanted = self.wanted()
        todo = []
        for p in self.db.q("SELECT * FROM posts WHERE status NOT IN ('locked', 'gone') AND url IS NOT NULL "
                           "ORDER BY published DESC, id DESC"):
            if p["status"] != "complete" or p.get("refetch") or any(wanted.get(k) for k in (p["skipped"] or [])):
                todo.append(p)
        self.status.set(phase="archiving posts", total=len(todo), done=0)
        archived = 0
        for n, post in enumerate(todo, 1):
            if self.stop_event.is_set():
                raise SiteError("stopped")
            self.status.set(done=n - 1, detail=post["title"], current=None)
            first_time = post["status"] == "new"
            try:
                if post.get("refetch") == "files":
                    post = self.forget_files(post)
                self.archive_post(post, wanted)
                archived += 1
                fresh = self.db.post(post["id"])
                if first_time and fresh["status"] in ("complete", "partial") and not fresh["pair_id"]:
                    self.new_titles.append(fresh["title"])
                    self.db.event("new_item", "", post["id"], fresh["title"])
                if first_time:
                    self.grow_archive_size(fresh)
            except (SessionExpired, LoginFailed, Blocked):
                raise
            except NotFound as e:
                self.db.update("posts", "id", post["id"], {"status": "gone", "problems": [str(e)]})
            except SiteError as e:
                if self.stop_event.is_set():
                    raise
                self.db.update("posts", "id", post["id"], {"status": "error", "problems": [str(e)]})
            except Exception as e:
                log.exception("post %s failed", post["id"])
                self.db.update("posts", "id", post["id"], {"status": "error",
                                                          "problems": [f"{type(e).__name__}: {e}"]})
        return archived

    def forget_files(self, post: dict) -> dict:
        """Delete what was downloaded for a post (video, audio, images, attachments, documents, thumbnail), so it's
        downloaded again. The saved page, card and comment pages stay until they're replaced."""
        rels = set((post.get("files") or {}).values())
        media = post.get("media") or {}
        if (media.get("audio") or {}).get("path"):
            rels.add(media["audio"]["path"])
        rels |= {i.get("url") for i in post.get("images") or []}
        rels |= {a.get("url") for a in post.get("attachments") or []}
        # Google Docs can be linked from several posts; keep the ones another post still uses
        for doc in post.get("documents") or []:
            url = doc.get("url")
            if url and not self.db.one("SELECT 1 FROM posts WHERE id != ? AND documents LIKE ? LIMIT 1",
                                       (post["id"], f"%{url}%")):
                rels.add(url)
        rels.add(post.get("thumbnail"))
        for rel in filter(None, rels):
            f = self.paths.archive / rel
            if f.is_file() and self.paths.archive.resolve() in f.resolve().parents:
                f.unlink()
        video = (media.get("video") or {}).get("path")
        if video:
            v = self.paths.media / video
            for side in v.parent.glob(glob_escape(v.stem) + "*"):
                if side.is_file():
                    side.unlink()
        fields = {"files": {}, "media": {}, "images": [], "attachments": [], "documents": [], "thumbnail": None}
        self.db.update("posts", "id", post["id"], fields)
        return {**post, **fields}

    def line_up(self) -> int:
        """Measure video and audio files that have no length yet, and work out how far each episode's video and
        audio version are apart (sync). Only pairs that are new or whose files changed are compared again."""
        posts = self.db.q("SELECT id, media FROM posts WHERE media IS NOT NULL AND media != '{}'")
        for p in posts:
            media = p["media"] or {}
            changed = False
            for part, root in (("video", self.paths.media), ("audio", self.paths.archive)):
                m = media.get(part)
                if m and m.get("path") and not m.get("duration"):
                    m["duration"] = probe_duration(root / m["path"])
                    changed = changed or bool(m["duration"])
            if changed:
                self.db.update("posts", "id", p["id"], {"media": media})
        pairs = []
        for primary in self.db.q("SELECT * FROM posts WHERE pair_id IS NULL AND id IN (SELECT pair_id FROM posts)"):
            members = [primary] + self.db.q("SELECT * FROM posts WHERE pair_id=? ORDER BY id", (primary["id"],))
            video = next(((m["id"], (m["media"] or {})["video"]) for m in members
                          if (m["media"] or {}).get("video") and (self.paths.media / m["media"]["video"]["path"]).exists()), None)
            audio = next(((m["id"], (m["media"] or {})["audio"]) for m in members
                          if (m["media"] or {}).get("audio") and (self.paths.archive / m["media"]["audio"]["path"]).exists()), None)
            if not video or not audio:
                continue
            key = {"video_post": video[0], "video_size": video[1].get("size"),
                   "audio_post": audio[0], "audio_size": audio[1].get("size")}
            old = primary.get("sync") or {}
            if all(old.get(k) == v for k, v in key.items()):
                continue
            pairs.append((primary, video, audio, key))
        if not pairs:
            return 0
        self.status.set(phase="lining up video and audio", detail="", done=0, total=len(pairs), current=None)
        for n, (primary, video, audio, key) in enumerate(pairs):
            if self.stop_event.is_set():
                break
            self.status.set(done=n, detail=primary["title"] or "")
            try:
                result = find_offset(self.paths.media / video[1]["path"], self.paths.archive / audio[1]["path"],
                                     video[1].get("duration"), audio[1].get("duration"))
            except Exception as e:  # a broken file shouldn't stop the check
                log.warning("couldn't line up %s: %s", primary["id"], e)
                result = {"offset": 0.0, "checked": False, "score": 0.0}
            self.db.update("posts", "id", primary["id"], {"sync": {**result, **key, "at": now()}})
        return len(pairs)

    def grow_archive_size(self, post: dict) -> None:
        """Add a newly archived post to the archive size shown on the admin page. The full count at the end of
        each check corrects any difference."""
        try:
            size = folder_size(self.paths.archive / "posts" / str(post["id"]))
            video = ((post.get("media") or {}).get("video") or {}).get("path")
            if video and (self.paths.media / video).is_file():
                size += (self.paths.media / video).stat().st_size
            self.db.set_setting("archive_bytes", int(self.db.setting("archive_bytes") or 0) + size)
        except (OSError, ValueError, TypeError):
            pass

    def fetch_post_page(self, post: dict):
        """(final URL, page HTML, page soup, card on the page or None). None if the post is locked."""
        final, text = self.page(post["url"])
        if "/pricing" in urlparse(final).path or "/payments/" in urlparse(final).path:
            self.db.update("posts", "id", post["id"], {"status": "locked", "locked": True})
            return None
        soup = P.soup_of(text)
        if not logged_in(soup):
            self.ensure_login()
            final, text = self.client.page(post["url"])
            soup = P.soup_of(text)
            if not logged_in(soup):
                raise SessionExpired("the site logged us out and logging in again didn't help")
        write_gz(self.post_dir(post["id"]) / "page.html.gz", text)
        frame = P.find_post_scope(soup, post["id"])
        if frame is not None and frame.select_one('a[href*="/available_tiers"]'):
            self.db.update("posts", "id", post["id"], {"status": "locked", "locked": True})
            return None
        return final, text, soup, frame

    def cached_card(self, post_id: int):
        f = self.paths.archive / "posts" / str(post_id) / "card.html.gz"
        if not f.exists():
            return None
        soup = P.soup_of(read_gz(f))
        return soup.find("turbo-frame") or soup

    def read_post(self, post: dict, final: str, soup, frame) -> tuple[dict, list[dict], list[str]]:
        """Post fields, downloadable items and unresolved attachment names, from the page and the feed's card."""
        cached = self.cached_card(post["id"])
        card_frame = frame if frame is not None else cached
        page_main = P.watch_block(soup) or soup.find("main") or soup
        if card_frame is None and not page_main.find(["mux-player", "video", "audio", "iframe"]) \
                and not page_main.select_one(".rich-text, .post__body"):
            raise SiteError("couldn't read the post's page; it's saved as page.html.gz for checking")
        fields: dict = {}
        if card_frame is not None and card_frame.name == "turbo-frame":
            fresh = P.parse_card(card_frame, final)
            if fresh:
                for k in ("duration", "like_count", "comment_count", "access"):
                    if fresh.get(k) not in (None, "", []):
                        fields[k] = fresh[k]
                if fresh.get("tag_pills"):
                    fields["tag_ids"] = [str(t["id"]) for t in fresh["tag_pills"]]
                if fresh["title"] and not fresh["title"].startswith("Post "):
                    fields["title"] = fresh["title"]
                if fresh.get("published") and not post.get("published"):
                    fields["published"], fields["date_text"] = fresh["published"], fresh["date_text"]
        body_source = P.post_card(card_frame) if card_frame is not None else page_main
        body_html, body_text, links = P.extract_body(body_source)
        if not body_text and cached is not None and card_frame is not cached:
            body_html, body_text, links = P.extract_body(P.post_card(cached))
        fields.update(body_html=body_html, body_text=body_text, links=links)
        # Players on the page have fresh playback tokens; the feed's card fills in the rest
        items, unresolved = P.extract_media(frame if frame is not None else page_main, final, self.same_site, post["id"])
        if cached is not None and frame is None:
            keys = {i["key"] for i in items}
            extra, extra_unresolved = P.extract_media(cached, final, self.same_site, post["id"])
            has_thumb = any(i["kind"] == "thumbnail" for i in items)
            items += [i for i in extra if i["key"] not in keys and not (i["kind"] == "mux" and token_expired(i.get("token")))
                      and not (i["kind"] == "thumbnail" and has_thumb)]
            resolved = {i.get("filename") for i in items if i["kind"] == "attachment"}
            unresolved = sorted({*unresolved, *extra_unresolved} - resolved)
        return fields, items, unresolved

    def archive_post(self, post: dict, wanted: dict[str, bool]) -> None:
        got = self.fetch_post_page(post)
        if got is None:
            return
        final, text, soup, frame = got
        d = self.post_dir(post["id"])
        update: dict = {"last_fetched": now()}
        fields, items, unresolved = self.read_post(post, final, soup, frame)
        update.update(fields)
        files: dict = dict(post["files"] or {})
        media: dict = dict(post["media"] or {})
        images = list(post["images"] or [])
        attachments = list(post["attachments"] or [])
        documents = list(post["documents"] or [])
        embeds = [{"kind": i["kind"], "url": i["url"], "youtube_id": i.get("video_id")}
                  for i in items if i["kind"] in ("youtube", "embed")]
        failures, notes, skipped = [], [], set()
        downloader = Downloader(self.client, int(self.s["max_height"] or 0),
                                progress=lambda m, f=None: self.status.set(detail=f"{post['title']}: {m}", current=f))
        title_base = P.safe_filename(update.get("title") or post["title"], 90, keep_ext=False)
        direct_ids = {i["playback_id"] for i in items if i["kind"] == "mux" and any(
            f"/{i['playback_id']}/" in urlparse(x["url"]).path for x in items if x["kind"] == "download")}
        thumbnail = post.get("thumbnail")
        image_n = len(images)

        def have(key: str) -> bool:
            return key in files and (self.paths.archive / files[key]).exists()

        for it in items:
            cls = self.media_class(it)
            if cls and not wanted.get(cls, True):
                skipped.add(cls)
                continue
            key = it["key"]
            try:
                if it["kind"] == "image":
                    if have(key):
                        continue
                    image_n += 1
                    p = downloader.file(it["url"], d / "images", f"{image_n:02d}{P.ext_of(it['url']) or '.jpg'}",
                                        referer=final)
                    files[key] = self.rel(p)
                    images.append({"url": self.rel(p), "source": it["url"]})
                elif it["kind"] == "thumbnail":
                    if have(key):
                        continue
                    # A different picture than before (for example after a fix): replace the old file, which has
                    # the same name and would otherwise be kept
                    if thumbnail and (self.paths.archive / thumbnail).is_file():
                        (self.paths.archive / thumbnail).unlink()
                        for k in [k for k, v in files.items() if v == thumbnail]:
                            del files[k]
                    p = downloader.file(it["url"], d, thumb_name(it), referer=final)
                    files[key] = self.rel(p)
                    thumbnail = self.rel(p)
                elif it["kind"] == "download":
                    ext = P.ext_of(it.get("filename") or it["url"])
                    if cls == "audio":
                        if have(key):
                            continue
                        p = downloader.file(it["url"], d, it.get("filename") or f"{title_base}{ext or '.m4a'}",
                                            referer=final)
                        files[key] = self.rel(p)
                        media["audio"] = {"path": self.rel(p), "mime": mime_of(p), "size": p.stat().st_size,
                                          "duration": probe_duration(p)}
                    elif cls == "video":
                        if "video" in media and (self.paths.media / media["video"]["path"]).exists():
                            continue
                        p = downloader.file(it["url"], d, f"{title_base}{ext or '.mp4'}", referer=final)
                        self.store_video(post, update, media, p)
                    else:
                        if have(key):
                            continue
                        p = downloader.file(it["url"], d / "attachments", it.get("filename"), referer=final,
                                            reject_html=True)
                        files[key] = self.rel(p)
                        attachments.append({"name": p.name, "url": self.rel(p), "size": p.stat().st_size})
                elif it["kind"] in ("mux", "hls"):
                    pid = it.get("playback_id")
                    if pid and pid in direct_ids:
                        continue
                    if it.get("drm"):
                        notes.append(f"{cls} is DRM-protected, so it can't be saved")
                        continue
                    q = f"?token={it['token']}" if it.get("token") else ""
                    if it.get("audio_only"):
                        if "audio" in media and (self.paths.archive / media["audio"]["path"]).exists():
                            continue
                        p = None
                        if pid:
                            try:
                                p = downloader.file(f"{MUX_BASE}/{pid}/audio.m4a{q}", d, f"{title_base}.m4a",
                                                    referer=final)
                            except DownloadError:
                                p = None
                        if p is None:
                            url = it.get("url") or f"{MUX_BASE}/{pid}.m3u8{q}"
                            p = downloader.stream(url, d, title_base, referer=final, audio_only=True)
                        media["audio"] = {"path": self.rel(p), "mime": mime_of(p), "size": p.stat().st_size,
                                          "duration": probe_duration(p)}
                    else:
                        if "video" in media and (self.paths.media / media["video"]["path"]).exists():
                            continue
                        url = it.get("url") or f"{MUX_BASE}/{pid}.m3u8{q}"
                        p = downloader.stream(url, d, title_base, referer=final, audio_only=False)
                        self.store_video(post, update, media, p)
                elif it["kind"] in ("youtube", "embed"):
                    if not wanted.get("embeds"):
                        continue
                    if "video" in media and (self.paths.media / media["video"]["path"]).exists():
                        continue
                    p = downloader.stream(it["url"], d, title_base, referer=final, audio_only=False)
                    self.store_video(post, update, media, p)
                elif it["kind"] == "attachment":
                    if have(key):
                        continue
                    url = it["url"]
                    if "/api/" in urlparse(url).path and "download" in urlparse(url).path:
                        url = self.resolve_download(url, final)
                    p = downloader.file(url, d / "attachments", it.get("filename"), referer=final,
                                        method=it.get("method", "GET"), fields=it.get("fields"), reject_html=True)
                    files[key] = self.rel(p)
                    attachments.append({"name": p.name, "url": self.rel(p), "size": p.stat().st_size})
                elif it["kind"] == "gdoc":
                    if have(key):
                        continue
                    fmt = {"document": ("export?format=pdf", ".pdf"), "spreadsheets": ("export?format=xlsx", ".xlsx"),
                           "presentation": ("export/pdf", ".pdf")}[it["doc_type"]]
                    export = f"{GDOCS_BASE}/{it['doc_type']}/d/{it['doc_id']}/{fmt[0]}"
                    label = it.get("label") or f"Google doc {it['doc_id'][:10]}"
                    name = P.safe_filename(f"{label} [{it['doc_id'][:8]}]", 110, keep_ext=False) + fmt[1]
                    try:
                        p = downloader.file(export, self.paths.archive / "google-docs", name, referer=final,
                                            reject_html=True, tries=2)
                        files[key] = self.rel(p)
                        documents.append({"name": p.name, "url": self.rel(p), "source": it["url"]})
                    except DownloadError as e:
                        if "HTTP 4" in str(e) or "web page instead" in str(e):
                            notes.append(f"linked Google Doc not saved ({e}): {it['url']}")
                        else:
                            raise
                elif it["kind"] == "gdrive":
                    if have(key):
                        continue
                    try:
                        p = self.drive_file(downloader, it, final)
                        files[key] = self.rel(p)
                        documents.append({"name": p.name, "url": self.rel(p), "source": it["url"]})
                    except DownloadError as e:
                        if "HTTP 4" in str(e) or "web page instead" in str(e):
                            notes.append(f"linked Google Drive file not saved ({e}): {it['url']}")
                        else:
                            raise
                elif it["kind"] == "gdrive_folder":
                    notes.append(f"linked Google Drive folder not saved (folders can't be downloaded as a whole; "
                                 f"links to the files in it are saved): {it['url']}")
            except DownloadError as e:
                if self.stop_event.is_set():
                    raise
                if it["kind"] == "attachment" and "web page instead" in str(e):
                    notes.append(f"attachment '{it.get('filename') or it['url']}' couldn't be downloaded ({e})")
                else:
                    failures.append(f"{cls or it['kind']}: {e}")

        for name in unresolved:
            notes.append(f"attachment '{name}' is listed but no download link was found")

        if wanted.get("comments"):
            try:
                self.archive_comments(post["id"])
            except SiteError as e:
                if self.stop_event.is_set():
                    raise
                failures.append(f"comments: {e}")

        # Keep only images that still exist, without duplicates
        seen_img = set()
        images = [i for i in images if i["url"] not in seen_img and not seen_img.add(i["url"])]
        update.update(files=files, media=media, images=images, attachments=dedupe(attachments, "url"),
                      documents=dedupe(documents, "url"), embeds=embeds, thumbnail=thumbnail,
                      problems=failures + notes, skipped=sorted(skipped), refetch=None,
                      status="partial" if failures else "complete")
        self.db.update("posts", "id", post["id"], update)

    @staticmethod
    def media_class(it: dict) -> str | None:
        k = it["kind"]
        if k in ("image", "thumbnail"):
            return "images"
        if k in ("gdoc", "gdrive", "gdrive_folder"):
            return "gdocs"
        if k in ("youtube", "embed"):
            return "embeds"
        if k in ("mux", "hls"):
            return "audio" if it.get("audio_only") else "video"
        if k == "download":
            e = P.ext_of(it.get("filename") or it["url"])
            if e in P.AUDIO_EXTS:
                return "audio"
            if e in P.VIDEO_EXTS:
                return "video"
            return "attachments"
        return "attachments"

    def store_video(self, post: dict, update: dict, media: dict, file: Path) -> None:
        """Put a finished video in the Jellyfin layout."""
        row = self.db.post(post["id"])
        row.update({k: v for k, v in update.items() if k in ("title", "body_text", "duration", "published")})
        dest = organize.place_video(self.db, self.paths, row, file)
        media["video"] = {"path": dest.relative_to(self.paths.media).as_posix(), "size": dest.stat().st_size,
                          "height": probe_height(dest), "duration": probe_duration(dest)}

    # ------------------------------------------------------------------ comments

    def archive_comments(self, post_id: int) -> int:
        """Every comment and reply on a post, saved raw (comments_NNN.html.gz) and in the database."""
        d = self.post_dir(post_id)
        queue: list[tuple[str, str | None]] = [(P.comments_url(self.client.base, post_id), None)]
        fetched: set[str] = set()
        index: list[dict] = []
        while queue and len(index) < 2000:
            if self.stop_event.is_set():
                raise SiteError("stopped")
            url, parent = queue.pop(0)
            if url in fetched:
                continue
            fetched.add(url)
            _, chunk = self.page(url, accept=TURBO_ACCEPT)
            name = f"comments_{len(index) + 1:03d}.html.gz"
            write_gz(d / name, chunk)
            index.append({"file": name, "parent": parent, "url": url, "fetched": now()})
            soup = P.soup_of(chunk, turbo=True)
            nxt = P.next_comments_page(soup, url)
            if nxt and nxt not in fetched:
                queue.append((nxt, parent))
            for cid, rurl in P.reply_requests(soup, url):
                if rurl not in fetched:
                    queue.append((rurl, cid))
        for old in d.glob("comments_*.html.gz"):
            if old.name not in {i["file"] for i in index}:
                old.unlink()
        (d / "comments_index.json").write_text(json.dumps(index, indent=1))
        return self.store_comments(post_id, self.comment_parts(d, index), self.client.abs("/supporters"))

    @staticmethod
    def comment_parts(d: Path, index: list[dict]) -> list[tuple]:
        """Saved comment pages with when each was downloaded (older indexes: the file's time)."""
        parts = []
        for i in index:
            f = d / i["file"]
            if f.exists():
                fetched = i.get("fetched") or datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()
                parts.append((read_gz(f), i["parent"], fetched))
        return parts

    def resolve_download(self, url: str, referer: str) -> str:
        """Attachment buttons point at an API address that answers with the file's real (signed) address."""
        r = self.client.request("GET", url, accept="application/json, text/plain, */*", stream=True,
                                headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"})
        try:
            if r.status_code >= 400:
                raise DownloadError(f"HTTP {r.status_code} asking for the download address")
            ctype = r.headers.get("Content-Type", "")
            if "json" in ctype:
                found: list[str] = []

                def walk(o):
                    if isinstance(o, dict):
                        for k in ("url", "download_url", "signed_url", "file_url"):
                            if isinstance(o.get(k), str) and o[k].startswith("http"):
                                found.append(o[k])
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)
                    elif isinstance(o, str) and o.startswith("http"):
                        found.append(o)
                walk(r.json())
                if found:
                    return found[0]
                raise DownloadError("the download address answer had no link in it")
            if "text/plain" in ctype and r.text.strip().startswith("http"):
                return r.text.strip()
            if P.url_key(r.url) != P.url_key(url) and "text/html" not in ctype:
                return r.url
            raise DownloadError(f"unexpected answer asking for the download address ({ctype or 'no type'})")
        finally:
            r.close()

    def store_comments(self, post_id: int, parts: list, page_url: str) -> int:
        comments = P.parse_comments(parts, page_url)
        # The site shows older comments' ages more coarsely ("11h", later "5mo"). Keep a date from an earlier, more
        # exact reading instead of replacing it with a rougher one.
        known = {r["id"]: r for r in self.db.q("SELECT id, date, date_text FROM comments WHERE post_id=?", (post_id,))}
        for c in comments:
            old = known.get(f"{post_id}:{c['id']}")
            if old and old["date"] and P.date_precision(old["date_text"]) < P.date_precision(c.get("date_text")):
                c["date"], c["date_text"] = old["date"], old["date_text"]
        # Pictures first, so the database isn't held while they download
        for c in comments:
            c["author_avatar"] = self.small_image(c.get("author_avatar"), "avatars")
            for b in c.get("author_badges") or []:
                b["image"] = self.small_image(b.get("image"), "badges")
        if comments or self.db.one("SELECT 1 FROM comments WHERE post_id=? LIMIT 1", (post_id,)):
            with self.db.lock:
                self.db.x("DELETE FROM comments WHERE post_id=?", (post_id,))
                for c in comments:
                    row = {k: v for k, v in c.items() if k not in ("pinned", "link")}
                    self.db.upsert("comments", "id", {**row, "id": f"{post_id}:{c['id']}", "post_id": post_id,
                                                      "parent_id": f"{post_id}:{c['parent_id']}" if c["parent_id"] else None,
                                                      "pinned": c.get("pinned", False), "link": c.get("link")})
        self.db.update("posts", "id", post_id, {"comments_fetched": now()})
        return len(comments)

    def small_image(self, url: str | None, folder: str) -> str | None:
        """Save a profile picture or badge once (they repeat a lot) and return its archive path.
        Keeps the web address if it can't be downloaded."""
        if not url or not url.startswith("http"):
            return url
        return self.save_asset(url, self.paths.archive / folder) or url

    def refresh_comments(self) -> None:
        days = int(self.s["comment_refresh_days"] or 0)
        if days <= 0:
            return
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")
        posts = self.db.q("SELECT * FROM posts WHERE status='complete' AND published >= ? AND "
                          "(comments_fetched IS NULL OR comments_fetched < ?)", (since, cutoff))
        self.status.set(phase="refreshing comments", total=len(posts), done=0)
        for n, post in enumerate(posts, 1):
            if self.stop_event.is_set():
                raise SiteError("stopped")
            self.status.set(done=n - 1, detail=post["title"])
            try:
                self.archive_comments(post["id"])
            except (SessionExpired, LoginFailed, Blocked):
                raise
            except SiteError as e:
                if self.stop_event.is_set():
                    raise
                log.warning("comment refresh failed for %s: %s", post["id"], e)

    # ------------------------------------------------------------------ public pages

    def archive_pages(self, feed_soup) -> None:
        if feed_soup is None:
            return
        self.status.set(phase="saving site pages", detail="", done=0, total=0)
        site_dir = self.paths.archive / "site"
        for url in P.site_assets(feed_soup, self.client.abs("/supporters")):
            self.save_asset(url, site_dir / "assets")
        logo = None
        header = feed_soup.find("header")
        if header and header.find("img", src=True):
            src = urljoin(self.client.abs("/supporters"), header.find("img", src=True)["src"])
            logo = self.rel(site_dir / "assets" / asset_name(src))
        self.db.set_setting("site_logo", logo)
        # The site's own share picture and logo aren't any show's art, even when a show page carries them
        site_pics = [u for u in (P.og_image(feed_soup, self.client.abs("/supporters")),
                                 logo and src) if u]
        if site_pics:
            known = set(self.db.setting("shared_show_art") or [])
            self.db.set_setting("shared_show_art", sorted(known | {asset_name(u) for u in site_pics}))

        links = P.nav_page_links(feed_soup, self.client.abs("/supporters"), self.same_site)
        shows = {s["page_url"].rstrip("/").rsplit("/", 1)[-1]: s for s in self.db.q("SELECT * FROM shows")
                 if s.get("page_url")}
        self.status.set(total=len(links))
        for n, url in enumerate(links, 1):
            self.status.set(done=n - 1, detail=urlparse(url).path)
            path = urlparse(url).path.rstrip("/") or "/"
            row = self.db.one("SELECT fetched FROM pages WHERE path=?", (path,))
            if row and row["fetched"] and datetime.fromisoformat(row["fetched"]) > datetime.now(timezone.utc) - PAGES_EVERY:
                continue
            try:
                final, text = self.page(url)
            except NotFound:
                continue
            except (SessionExpired, LoginFailed, Blocked):
                raise
            except SiteError as e:
                log.warning("page %s failed: %s", url, e)
                continue
            self.store_site_page(path, final, text, shows)

    def store_site_page(self, path: str, final: str, text: str, shows: dict, fetched: str | None = None) -> None:
        """Save one site page and what it says about a show. Also used to re-read saved pages."""
        soup = P.soup_of(text)
        slug = P.slugify(path.strip("/").replace("/", "-")) or "home"
        if fetched is None:
            write_gz(self.paths.archive / "pages" / f"{slug}.html.gz", text)
        main = P.main_content(soup)
        mapping = {}
        for img in P.page_images(main, final):
            local = self.save_asset(img, self.paths.archive / "pages" / "assets")
            if local:
                mapping[img] = local
        html = str(main)
        for remote, local in mapping.items():
            html = html.replace(remote, "/files/" + local).replace(remote.replace("&", "&amp;"), "/files/" + local)
        self.db.upsert("pages", "path", {"path": path, "title": P.page_title(soup), "file": f"pages/{slug}.html.gz",
                                         "fetched": fetched or now(), "main_html": html})
        key = path.rsplit("/", 1)[-1]
        if path.startswith("/pages/") and key in shows:
            show = shows[key]
            desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
            poster_url = P.og_image(soup, final)
            shared = set(self.db.setting("shared_show_art") or [])
            current = self.db.one("SELECT art_source FROM shows WHERE id=?", (show["id"],)) or {}
            fields, options = {}, []
            if desc and desc.get("content"):
                fields["description"] = P.norm_space(desc["content"])
            # The page's own share picture is the show's picture: the creators set it for this page. Once a show has
            # a picture it only changes for another such picture, and never when the user picked one.
            if poster_url and asset_name(poster_url) not in shared:
                local = self.save_asset(poster_url, self.paths.archive / "shows" / show["id"])
                if local:
                    fields["poster"] = local
                    options.append(local)
                    if current.get("art_source") != "picked":
                        fields.update(art=local, art_source="page")
            # Other pictures on the page are only offered on the admin page, to pick from
            imgs = [i for i in P.page_images(main, final) if asset_name(i) not in shared]
            for img in imgs:
                local = mapping.get(img) or self.save_asset(img, self.paths.archive / "shows" / show["id"])
                if local and local not in options:
                    options.append(local)
            if imgs and options:
                fields["banner"] = mapping.get(imgs[0]) or options[-1]
            fields["art_options"] = options
            self.db.update("shows", "id", show["id"], fields)

    def save_asset(self, url: str, folder: Path) -> str | None:
        local = folder / asset_name(url)
        if local.exists():
            return self.rel(local)
        try:
            r = self.client.request("GET", url, pace=self.same_site(url), tries=2, timeout=(10, 60))
        except SiteError:
            return None
        if r.status_code != 200 or "text/html" in r.headers.get("Content-Type", ""):
            return None
        folder.mkdir(parents=True, exist_ok=True)
        local.write_bytes(r.content)
        return self.rel(local)

    # ------------------------------------------------------------------ re-reading saved pages

    def run_reparse(self) -> dict:
        """Re-read everything saved, as its own job with status and a Stop button. Run by the scheduler, so it
        never overlaps a check."""
        self.stop_event.clear()
        self.status.set(running=True, phase="re-reading saved pages", detail="", done=0, total=0, current=None,
                        started=now())
        try:
            return self.reparse()
        finally:
            self.status.set(running=False, phase="idle", detail="", done=0, total=0, current=None)

    def reparse(self) -> dict:
        """Re-read everything saved with the current parsers: post pages and feed cards, comment pages, the feed
        (tags and the Shows menu) and site pages.

        Nothing big is downloaded again. Small pictures that weren't saved yet (badges, profile pictures) are.
        Posts where the re-read finds files that were never downloaded, because an older version missed them,
        are marked so the next check fetches just those posts again."""
        posts = self.db.q("SELECT * FROM posts ORDER BY published DESC, id DESC")
        wanted = self.wanted()
        self.status.set(phase="re-reading saved pages", total=len(posts), done=0)
        n = to_fetch = 0
        stopped = False
        for i, post in enumerate(posts):
            if self.stop_event.is_set():
                stopped = True
                break
            self.status.set(done=i, detail=post["title"] or str(post["id"]))
            d = self.paths.archive / "posts" / str(post["id"])
            page_file = d / "page.html.gz"
            final = post["url"] or self.client.abs("/supporters")
            if page_file.exists():
                soup = P.soup_of(read_gz(page_file))
                frame = P.find_post_scope(soup, post["id"])
                try:
                    fields, items, _ = self.read_post(post, final, soup, frame)
                except SiteError:
                    fields, items = {}, []
                # A thumbnail that's now read differently (like an audio post's cover instead of the blurred strip
                # behind it) is small: fetch it right away instead of waiting for the next check
                thumb = next((i for i in items if i["kind"] == "thumbnail"), None)
                files = dict(post["files"] or {})
                if (thumb and wanted.get("images", True) and post["status"] in ("complete", "partial")
                        and not (thumb["key"] in files and (self.paths.archive / files[thumb["key"]]).exists())):
                    rel = self.save_thumbnail(post, thumb)
                    if rel:
                        files = {k: v for k, v in files.items() if not k.startswith("thumb:")}
                        files[thumb["key"]] = rel
                        fields.update(files=files, thumbnail=rel)
                if post["status"] in ("complete", "partial"):
                    missing = self.missing_classes({**post, **fields}, items, wanted)
                    if missing:
                        # archive_posts picks up posts whose skipped kinds are wanted, and fetches only what's missing
                        fields["skipped"] = sorted(set(post["skipped"] or []) | missing)
                        to_fetch += 1
                if fields:
                    self.db.update("posts", "id", post["id"], fields)
            index_file = d / "comments_index.json"
            if index_file.exists():
                index = json.loads(index_file.read_text())
                self.store_comments(post["id"], self.comment_parts(d, index), self.client.abs("/supporters"))
            n += 1
        pages = 0
        if not stopped:
            self.status.set(phase="re-reading saved pages", detail="feed and site pages", done=len(posts))
            feed = self.paths.archive / "site" / "feed.html.gz"
            if feed.exists():
                soup = P.soup_of(read_gz(feed))
                self.save_tags(P.parse_tags(soup))
                organize.merge_show_menu(self.db, P.parse_show_menu(soup, self.client.abs("/supporters")))
            shows = {s["page_url"].rstrip("/").rsplit("/", 1)[-1]: s for s in self.db.q("SELECT * FROM shows")
                     if s.get("page_url")}
            for row in self.db.q("SELECT * FROM pages"):
                f = self.paths.archive / (row["file"] or "")
                if row["file"] and f.exists():
                    self.store_site_page(row["path"], self.client.abs(row["path"]), read_gz(f), shows,
                                         fetched=row["fetched"])
                    pages += 1
            organize.drop_shared_show_art(self.db)
        organize.classify_all(self.db)
        organize.reorganize(self.db, self.paths)
        organize.refresh_jellyfin(self.db, self.paths)
        removed = 0 if stopped else self.remove_unused_pictures()
        result = {"finished": now(), "posts": n, "pages": pages, "to_fetch": to_fetch, "removed": removed,
                  "stopped": stopped}
        self.db.set_setting("last_reparse", result)
        if not stopped:
            self.db.set_setting("parser_version", P.PARSER_VERSION)
        return result

    def save_thumbnail(self, post: dict, it: dict) -> str | None:
        """Download a post's thumbnail and replace the old one. Each picture gets its own name, so browsers don't
        keep showing an old copy."""
        dest = self.post_dir(post["id"]) / thumb_name(it)
        if not dest.exists():
            try:
                r = self.client.request("GET", it["url"], pace=self.same_site(it["url"]), tries=2, timeout=(10, 30))
            except SiteError:
                return None
            if r.status_code != 200 or "text/html" in r.headers.get("Content-Type", ""):
                return None
            dest.write_bytes(r.content)
        old = post.get("thumbnail")
        if old and old != self.rel(dest):
            (self.paths.archive / old).unlink(missing_ok=True)
        return self.rel(dest)

    def drive_file(self, downloader: Downloader, it: dict, referer: str) -> Path:
        """A file someone shared from Google Drive. Large files get a page asking to confirm the download, as Drive
        can't scan them for viruses; its form has what's needed to go ahead."""
        dest = self.paths.archive / "google-docs" / "drive" / it["file_id"]
        url = f"{DRIVE_DOWNLOAD}?{urlencode({'id': it['file_id'], 'export': 'download'})}"
        try:
            p = downloader.file(url, dest, None, referer=referer, reject_html=True, tries=2)
        except DownloadError as e:
            if "web page instead" not in str(e):
                raise
            try:
                r = self.client.request("GET", url, headers={"Referer": referer}, pace=False, tries=2)
            except Exception:
                raise e from None
            form = P.soup_of(r.text).find("form", action=True)
            fields = [(i["name"], i.get("value", "")) for i in form.find_all("input", attrs={"name": True})] if form else []
            if not form or not any(k == "confirm" for k, _ in fields):
                raise
            p = downloader.file(f"{urljoin(r.url, form['action'])}?{urlencode(fields)}", dest, None, referer=url,
                                reject_html=True, tries=2)
        # Drive names the file; when it doesn't, use the name the post showed for it
        label = P.safe_filename(it.get("label") or "", 110)
        if p.name == "download" or not p.suffix:
            better = p.with_name(label if P.ext_of(label) else f"{label or 'Drive file ' + it['file_id'][:8]}.bin")
            p = p.rename(better)
        return p

    def missing_classes(self, post: dict, items: list[dict], wanted: dict) -> set[str]:
        """Kinds of file that a saved page lists but that were never downloaded. Follows the same rules as
        archive_post for what counts as already saved."""
        files = post.get("files") or {}
        media = post.get("media") or {}

        def have(key: str) -> bool:
            return key in files and (self.paths.archive / files[key]).exists()

        def have_video() -> bool:
            v = media.get("video") or {}
            return bool(v.get("path")) and (self.paths.media / v["path"]).exists()

        def have_audio() -> bool:
            a = media.get("audio") or {}
            return bool(a.get("path")) and (self.paths.archive / a["path"]).exists()

        direct_ids = {i["playback_id"] for i in items if i["kind"] == "mux" and any(
            f"/{i['playback_id']}/" in urlparse(x["url"]).path for x in items if x["kind"] == "download")}
        missing: set[str] = set()
        for it in items:
            cls = self.media_class(it)
            if cls and not wanted.get(cls, True):
                continue
            kind = it["kind"]
            if kind in ("image", "thumbnail", "attachment", "gdoc", "gdrive"):
                gone = not have(it["key"])
            elif kind == "download":
                gone = not have_video() if cls == "video" else not have(it["key"])
            elif kind in ("mux", "hls"):
                if it.get("drm") or (it.get("playback_id") and it["playback_id"] in direct_ids):
                    continue
                gone = not (have_audio() if it.get("audio_only") else have_video())
            elif kind in ("youtube", "embed"):
                gone = bool(wanted.get("embeds")) and not have_video()
            else:
                continue
            if gone and cls:
                missing.add(cls)
        return missing

    def remove_unused_pictures(self) -> int:
        """Delete profile pictures and badges that no saved comment uses any more, for example ones an older
        version saved by mistake."""
        used: set[str] = set()
        for r in self.db.q("SELECT author_avatar, author_badges FROM comments"):
            if r["author_avatar"]:
                used.add(r["author_avatar"])
            for b in r["author_badges"] or []:
                if b.get("image"):
                    used.add(b["image"])
        removed = 0
        for folder in ("avatars", "badges"):
            root = self.paths.archive / folder
            if not root.is_dir():
                continue
            for f in root.iterdir():
                if f.is_file() and self.rel(f) not in used:
                    f.unlink(missing_ok=True)
                    removed += 1
        return removed


def thumb_name(it: dict) -> str:
    """A thumbnail's file name, different for each picture."""
    return f"thumbnail-{hashlib.sha1(it['key'].encode()).hexdigest()[:8]}{P.ext_of(it['url']) or '.jpg'}"


def token_expired(token: str | None) -> bool:
    """Mux playback tokens are signed and last about a day."""
    if not token:
        return False
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return bool(exp) and exp < time.time() + 600
    except (IndexError, ValueError):
        return False


def folder_size(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def dedupe(items: list[dict], key: str) -> list[dict]:
    seen, out = set(), []
    for i in items:
        if i.get(key) not in seen:
            seen.add(i.get(key))
            out.append(i)
    return out


def mime_of(path: Path) -> str:
    return {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac",
            ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac"}.get(
        path.suffix.lower(), "application/octet-stream")
