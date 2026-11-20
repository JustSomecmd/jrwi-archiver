"""One check of the members site: read the feed, archive new and changed posts, refresh comments."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from . import organize
from .config import MUX_BASE, SITE_BASE, Paths
from .db import Db, now
from .site import parse as P
from .site.client import (HTML_ACCEPT, TURBO_ACCEPT, Blocked, Client, LoginFailed, NotFound, SessionExpired,
                          SiteError, logged_in)
from .site.download import Downloader, DownloadError, probe_height

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
    started: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "phase": self.phase, "detail": self.detail,
                    "done": self.done, "total": self.total, "started": self.started}


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
        self.status.set(phase="logging in", detail="")
        self.client.login(email, password, debug_file=self.paths.data / "login-debug.txt")

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
            organize.classify_all(self.db)
            archived = self.archive_posts()
            if s["comments"]:
                self.refresh_comments()
            if s["public_pages"]:
                self.archive_pages(feed_soup)
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
            self.status.set(running=False, phase="idle", detail="", done=0, total=0)
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
            if p["status"] != "complete" or any(wanted.get(k) for k in (p["skipped"] or [])):
                todo.append(p)
        self.status.set(phase="archiving posts", total=len(todo), done=0)
        archived = 0
        for n, post in enumerate(todo, 1):
            if self.stop_event.is_set():
                raise SiteError("stopped")
            self.status.set(done=n - 1, detail=post["title"])
            first_time = post["status"] == "new"
            try:
                self.archive_post(post, wanted)
                archived += 1
                fresh = self.db.post(post["id"])
                if first_time and fresh["status"] in ("complete", "partial") and not fresh["pair_id"]:
                    self.new_titles.append(fresh["title"])
                    self.db.event("new_item", "", post["id"], fresh["title"])
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

    def fetch_post_page(self, post: dict) -> tuple[str, str, object, object] | None:
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
        scope = P.find_post_scope(soup, post["id"])
        if scope is None:
            raise SiteError("couldn't find the post on its page (the site layout may have changed)")
        if scope.select_one('a[href*="/available_tiers"]'):
            self.db.update("posts", "id", post["id"], {"status": "locked", "locked": True})
            return None
        write_gz(self.post_dir(post["id"]) / "page.html.gz", text)
        return final, text, soup, scope

    def archive_post(self, post: dict, wanted: dict[str, bool]) -> None:
        got = self.fetch_post_page(post)
        if got is None:
            return
        final, text, soup, scope = got
        d = self.post_dir(post["id"])
        update: dict = {"last_fetched": now()}

        if scope.name == "turbo-frame":
            fresh = P.parse_card(scope, final)
            if fresh:
                for k in ("duration", "like_count", "comment_count", "access", "tag_ids"):
                    if fresh.get(k) not in (None, "", []):
                        update[k] = fresh[k]
                if fresh["title"] and not fresh["title"].startswith("Post "):
                    update["title"] = fresh["title"]
                if fresh.get("published") and not post.get("published"):
                    update["published"] = fresh["published"]
                    update["date_text"] = fresh["date_text"]
        card = P.post_card(scope) if scope.name == "turbo-frame" else scope
        body_html, body_text, links = P.extract_body(card)
        update.update(body_html=body_html, body_text=body_text, links=links)

        items, unresolved = P.extract_media(scope, final, self.same_site)
        files: dict = dict(post["files"] or {})
        media: dict = dict(post["media"] or {})
        images = list(post["images"] or [])
        attachments = list(post["attachments"] or [])
        documents = list(post["documents"] or [])
        embeds = [{"kind": i["kind"], "url": i["url"], "youtube_id": i.get("video_id")}
                  for i in items if i["kind"] in ("youtube", "embed")]
        failures, notes, skipped = [], [], set()
        downloader = Downloader(self.client, int(self.s["max_height"] or 0),
                                progress=lambda m: self.status.set(detail=f"{post['title']}: {m}"))
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
                    p = downloader.file(it["url"], d, f"thumbnail{P.ext_of(it['url']) or '.jpg'}", referer=final)
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
                        media["audio"] = {"path": self.rel(p), "mime": mime_of(p), "size": p.stat().st_size}
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
                        media["audio"] = {"path": self.rel(p), "mime": mime_of(p), "size": p.stat().st_size}
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
                    p = downloader.file(it["url"], d / "attachments", it.get("filename"), referer=final,
                                        method=it.get("method", "GET"), fields=it.get("fields"), reject_html=True)
                    files[key] = self.rel(p)
                    attachments.append({"name": p.name, "url": self.rel(p), "size": p.stat().st_size})
                elif it["kind"] == "gdoc":
                    if have(key):
                        continue
                    fmt = {"document": ("export?format=pdf", ".pdf"), "spreadsheets": ("export?format=xlsx", ".xlsx"),
                           "presentation": ("export/pdf", ".pdf")}[it["doc_type"]]
                    export = f"https://docs.google.com/{it['doc_type']}/d/{it['doc_id']}/{fmt[0]}"
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
                self.archive_comments(post["id"], final, text, soup)
            except SiteError as e:
                if self.stop_event.is_set():
                    raise
                failures.append(f"comments: {e}")

        # Keep only images that still exist, without duplicates
        seen_img = set()
        images = [i for i in images if i["url"] not in seen_img and not seen_img.add(i["url"])]
        update.update(files=files, media=media, images=images, attachments=dedupe(attachments, "url"),
                      documents=dedupe(documents, "url"), embeds=embeds, thumbnail=thumbnail,
                      problems=failures + notes, skipped=sorted(skipped),
                      status="partial" if failures else "complete")
        self.db.update("posts", "id", post["id"], update)

    @staticmethod
    def media_class(it: dict) -> str | None:
        k = it["kind"]
        if k in ("image", "thumbnail"):
            return "images"
        if k == "gdoc":
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
                          "height": probe_height(dest)}

    # ------------------------------------------------------------------ comments

    def archive_comments(self, post_id: int, final: str, text: str, soup) -> None:
        d = self.post_dir(post_id)
        parts = [text]
        fetched: set[str] = set()
        # (Turbo frame id or None, URL): comment threads that load separately, and "more comments" links
        queue = [(fid, u) for fid, u in P.lazy_frames(soup, final, self.same_site, skip_ids={f"post_{post_id}"})
                 if "comment" in (u + fid).lower()]
        queue += [(None, u) for u in P.load_more_comment_links(soup, final, self.same_site)]
        n = 0
        while queue and n < 200:
            frame_id, url = queue.pop(0)
            if url in fetched:
                continue
            fetched.add(url)
            _, chunk = self.page(url, accept=TURBO_ACCEPT, headers={"Turbo-Frame": frame_id} if frame_id else None)
            n += 1
            write_gz(d / f"comments_{n:03d}.html.gz", chunk)
            parts.append(chunk)
            chunk_soup = P.soup_of(chunk, turbo=True)
            for u in P.load_more_comment_links(chunk_soup, url, self.same_site):
                if u not in fetched:
                    queue.append((None, u))
            for fid, u in P.lazy_frames(chunk_soup, url, self.same_site):
                if "comment" in (u + fid).lower() and u not in fetched:
                    queue.append((fid, u))
        for old in d.glob("comments_*.html.gz"):
            if int(old.stem.split("_")[1].split(".")[0]) > n:
                old.unlink()
        self.store_comments(post_id, parts, final)

    def store_comments(self, post_id: int, parts: list[str], page_url: str) -> None:
        comments = P.parse_comments(parts, page_url)
        avatars_dir = self.paths.archive / "avatars"
        if comments or self.db.one("SELECT 1 FROM comments WHERE post_id=? LIMIT 1", (post_id,)):
            with self.db.lock:
                self.db.x("DELETE FROM comments WHERE post_id=?", (post_id,))
                for c in comments:
                    avatar = c.get("author_avatar")
                    if avatar and avatar.startswith("http"):
                        local = avatars_dir / asset_name(avatar)
                        if not local.exists():
                            try:
                                r = self.client.request("GET", avatar, pace=False, tries=2, timeout=(10, 30))
                                if r.status_code == 200:
                                    avatars_dir.mkdir(parents=True, exist_ok=True)
                                    local.write_bytes(r.content)
                            except SiteError:
                                pass
                        c["author_avatar"] = self.rel(local) if local.exists() else avatar
                    self.db.upsert("comments", "id", {**c, "id": f"{post_id}:{c['id']}", "post_id": post_id,
                                                      "parent_id": f"{post_id}:{c['parent_id']}" if c["parent_id"] else None})
        self.db.update("posts", "id", post_id, {"comments_fetched": now()})

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
                got = self.fetch_post_page(post)
                if got is None:
                    continue
                final, text, soup, scope = got
                if scope.name == "turbo-frame":
                    fresh = P.parse_card(scope, final)
                    if fresh:
                        self.db.update("posts", "id", post["id"], {k: fresh[k] for k in ("like_count", "comment_count")
                                                                   if fresh.get(k) is not None})
                self.archive_comments(post["id"], final, text, soup)
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
            soup = P.soup_of(text)
            slug = P.slugify(path.strip("/").replace("/", "-")) or "home"
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
                                             "fetched": now(), "main_html": html})
            key = path.rsplit("/", 1)[-1]
            if path.startswith("/pages/") and key in shows:
                show = shows[key]
                desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
                poster_url = P.og_image(soup, final)
                fields = {}
                if desc and desc.get("content"):
                    fields["description"] = P.norm_space(desc["content"])
                if poster_url:
                    local = self.save_asset(poster_url, self.paths.archive / "shows" / show["id"])
                    if local:
                        fields["poster"] = local
                imgs = P.page_images(main, final)
                if imgs:
                    local = mapping.get(imgs[0]) or self.save_asset(imgs[0], self.paths.archive / "shows" / show["id"])
                    if local:
                        fields["banner"] = local
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

    def reparse(self) -> int:
        """Re-read every saved post page and comment page with the current parser. No downloads."""
        n = 0
        for post in self.db.q("SELECT id, url FROM posts"):
            d = self.paths.archive / "posts" / str(post["id"])
            page_file = d / "page.html.gz"
            if not page_file.exists():
                continue
            text = read_gz(page_file)
            soup = P.soup_of(text)
            scope = P.find_post_scope(soup, post["id"])
            if scope is None:
                continue
            final = post["url"] or self.client.abs("/supporters")
            card = P.post_card(scope) if scope.name == "turbo-frame" else scope
            body_html, body_text, links = P.extract_body(card)
            fields = {"body_html": body_html, "body_text": body_text, "links": links}
            if scope.name == "turbo-frame":
                fresh = P.parse_card(scope, final)
                if fresh:
                    fields.update({k: fresh[k] for k in ("like_count", "comment_count", "tag_ids")
                                   if fresh.get(k) is not None})
            self.db.update("posts", "id", post["id"], fields)
            parts = [text] + [read_gz(f) for f in sorted(d.glob("comments_*.html.gz"))]
            self.store_comments(post["id"], parts, final)
            n += 1
        feed = self.paths.archive / "site" / "feed.html.gz"
        if feed.exists():
            soup = P.soup_of(read_gz(feed))
            self.save_tags(P.parse_tags(soup))
            organize.merge_show_menu(self.db, P.parse_show_menu(soup, self.client.abs("/supporters")))
        organize.classify_all(self.db)
        organize.reorganize(self.db, self.paths)
        organize.refresh_jellyfin(self.db, self.paths)
        return n


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
