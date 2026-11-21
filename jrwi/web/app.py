"""The web server: the JSON API for the apps, the archive website and the admin pages."""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import NAME, VERSION
from ..config import SITE_BASE, Paths, new_token
from ..crawler import Archiver
from .. import files as files_mod
from .. import handover
from ..db import Db, now
from ..tubearchivist import TaError, TubeArchivist
from ..organize import (ART_SOURCES, GENERIC_TAG_TABS, MAX_PICTURE, TABS, add_show, art_choices, choose_show_art,
                        classify_all, delete_saved_override, drop_shared_show_art, norm, post_pictures, refresh_jellyfin,
                        reorganize, saved_overrides, set_art_override, show_tag_names, tag_role)
from ..scheduler import Scheduler
from .progress import Progress
from .serialize import Serializer, clean_html, files_url, iso, safe_join, tab_name

log = logging.getLogger("jrwi.web")
HERE = Path(__file__).parent


# --------------------------------------------------------------------------- passwords

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240_000).hex()
    return f"pbkdf2${salt}${digest}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240_000).hex()
    return hmac.compare_digest(test, digest)


def human_size(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"



def nice_date(value) -> str:
    """'2026-11-07' or a full ISO time as the site writes dates: 'Nov 7, 2026'."""
    if not value:
        return ""
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:10]
    return f"{d:%b} {d.day}, {d.year}"



def ago(value) -> str:
    """How long ago, the way the site writes it under comments: '4d', '2w', '5mo', '1y'."""
    if not value:
        return ""
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return ""
    days = (datetime.now(timezone.utc).date() - d).days
    if days < 1:
        return "today"
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def human_duration(s) -> str:
    if not s:
        return ""
    s = int(s)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# --------------------------------------------------------------------------- app factory

def create_app(paths: Paths | None = None, start_scheduler: bool = True) -> FastAPI:
    paths = paths or Paths.from_env()
    db = Db(paths.data / "jrwi.db")
    for key in ("api_token", "session_secret"):
        if not db.setting(key):
            db.set_setting(key, new_token())
    env_pw = os.environ.get("JRWI_WEB_PASSWORD")
    if env_pw and not db.setting("web_password_hash"):
        db.set_setting("web_password_hash", hash_password(env_pw))

    drop_shared_show_art(db)   # show art that was really the site's own picture, from before 0.4.1
    classify_all(db)           # sorting rules change between versions (0.6.0: by the site's tags)
    archiver = Archiver(db, paths)
    scheduler = Scheduler(archiver, db)
    ser = Serializer(db, paths)
    progress = Progress(db, ser)

    @asynccontextmanager
    async def lifespan(_app):
        if start_scheduler:
            scheduler.start()
        yield

    app = FastAPI(title="JRWI archiver", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=db.setting("session_secret"), max_age=60 * 60 * 24 * 30,
                       same_site="lax", session_cookie="jrwi_session")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(files_url=files_url, human_size=human_size, human_duration=human_duration,
                                 tab_name=tab_name, version=VERSION, quote=quote, nice_date=nice_date, ago=ago,
                                 version_name={"video": "Video version", "audio": "Audio version",
                                               "youtube": "YouTube version"})
    app.state.db, app.state.archiver, app.state.scheduler = db, archiver, scheduler

    # ------------------------------------------------------------------ auth

    def session_ok(request: Request) -> bool:
        return bool(request.session.get("auth")) and request.session.get("pw") == (db.setting("web_password_hash") or "")[-12:]

    def token_ok(request: Request) -> bool:
        token = db.setting("api_token")
        given = request.query_params.get("token")
        header = request.headers.get("Authorization", "")
        if header.lower().startswith(("token ", "bearer ")):
            given = header.split(" ", 1)[1].strip()
        return bool(token and given and hmac.compare_digest(given, token))

    def api_auth(request: Request) -> None:
        if not (token_ok(request) or session_ok(request)):
            raise HTTPException(401, "missing or wrong API token")

    class NeedLogin(Exception):
        pass

    def web_auth(request: Request) -> None:
        if not session_ok(request):
            raise NeedLogin()

    @app.exception_handler(NeedLogin)
    def _need_login(request: Request, exc: NeedLogin):
        if not db.setting("web_password_hash"):
            return RedirectResponse("/setup", 303)
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", 303)

    @app.exception_handler(HTTPException)
    def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/api/", "/files/")):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{escape(str(exc.detail))}</p><p><a href='/'>Home</a></p>",
                            status_code=exc.status_code)

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        shows = db.q("SELECT id, name FROM shows ORDER BY sort, name")
        return templates.TemplateResponse(request, name, {
            "shows_nav": shows, "logo": files_url(db.setting("site_logo")), "status": archiver.status.snapshot(),
            **ctx})

    # ------------------------------------------------------------------ API

    def status_obj() -> dict:
        snap = archiver.status.snapshot()
        last = db.one("SELECT * FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1")
        counts = db.one("SELECT COUNT(*) AS items, SUM(status IN ('complete','partial')) AS archived, "
                        "SUM(status='locked') AS locked, SUM(json_array_length(problems) > 0) AS problems FROM posts")
        return {
            "running": snap["running"], "step": snap["step"], "phase": snap["phase"], "detail": snap["detail"],
            "progress": {"done": snap["done"], "total": snap["total"], "current": snap["current"], "unit": snap["unit"]},
            "last_check_started": last["started"] if last else None,
            "last_check_finished": last["finished"] if last else None,
            "next_check": scheduler.next_check.isoformat(timespec="seconds") if scheduler.next_check else None,
            "last_result": last["result"] if last else None, "message": last["message"] if last else None,
            "login_ok": (last["result"] != "login_failed") if last else None,
            "counts": {k: int(counts[k] or 0) for k in ("items", "archived", "locked", "problems")},
            "disk": scheduler.disk(),
        }

    def item_out(primary: dict, full: bool = False) -> dict:
        """An item with its progress, in its main post's timeline (contract v0.10)."""
        members = ser.members(primary)
        out = (ser.full if full else ser.summary)(primary, members)
        out["progress"] = progress.of_item(primary, members, out["video"], out["audio"])
        return out

    def paged(rows_sql: str, args: list, page: int, page_size: int, one=None) -> dict:
        page, page_size = max(1, page), max(1, min(page_size, 200))
        total = db.one(f"SELECT COUNT(*) AS n FROM ({rows_sql})", args)["n"]
        rows = db.q(f"{rows_sql} LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size])
        return {"items": [(one or ser.summary)(r) for r in rows], "page": page, "page_size": page_size, "total": total}

    def item_query(show=None, tab=None, kind=None, q=None, order="desc", include_locked=False,
                   separate=False, youtube_id=None, season=None) -> tuple[str, list]:
        """Items group an episode's versions under its main post. separate=True lists every post on its own."""
        where, args = ([] if separate else ["pair_id IS NULL"]) + ["status != 'gone'"], []
        if not include_locked:
            where.append("status != 'locked'")
        if show and tab:
            # A post is in every show and tab its tags put it in (contract v0.12 and v0.13)
            where.append("((show_id = ? AND tab = ?) OR EXISTS (SELECT 1 FROM json_each(posts.places) pl "
                         "WHERE pl.value = ?))")
            args += [show, tab, f"{show}:{tab}"]
        elif show:
            where.append("(show_id = ? OR EXISTS (SELECT 1 FROM json_each(posts.places) pl WHERE pl.value LIKE ?))")
            args += [show, f"{show}:%"]
        elif tab:
            where.append("(tab = ? OR EXISTS (SELECT 1 FROM json_each(posts.tabs) t WHERE t.value = ?))")
            args += [tab, tab]
        if season is not None:
            # Seasons are the show's own: a post filed under another show has none of this one's
            where.append("COALESCE(season, 1) = ?" + (" AND show_id = ?" if show else ""))
            args += [season] + ([show] if show else [])
        if kind:
            where.append({"video": "(json_extract(media,'$.video.path') IS NOT NULL OR path_kind IN ('videos','video_embeds'))",
                          "audio": "(path_kind = 'audio' OR json_extract(media,'$.audio.path') IS NOT NULL "
                                   "OR id IN (SELECT pair_id FROM posts WHERE path_kind='audio'))",
                          "post": "path_kind = 'posts'"}.get(kind, "1=1"))
        if q:
            where.append("(title LIKE ? OR body_text LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if youtube_id:
            # Items with a post (any version) that embeds this YouTube video
            group = "e.id" if separate else "COALESCE(e.pair_id, e.id)"
            where.append(f"id IN (SELECT {group} FROM posts e, json_each(e.embeds) j "
                         "WHERE json_extract(j.value, '$.youtube_id') = ?)")
            args.append(youtube_id)
        direction = "ASC" if order == "asc" else "DESC"
        if tab == "episodes":
            order_by = f"COALESCE(season, 1) {direction}, episode {direction}, published {direction}, id {direction}"
        else:
            order_by = f"published {direction}, id {direction}"
        return f"SELECT * FROM posts WHERE {' AND '.join(where)} ORDER BY {order_by}", args

    @app.get("/api/ping")
    def api_ping():
        return {"name": NAME, "version": VERSION}

    @app.get("/api/status", dependencies=[Depends(api_auth)])
    def api_status():
        return JSONResponse(status_obj(), headers={"Cache-Control": "no-store"})

    @app.post("/api/check", dependencies=[Depends(api_auth)])
    def api_check(full: bool = False):
        if scheduler.check_now(full=full):
            return JSONResponse({"started": True}, status_code=202)
        return JSONResponse({"error": "a check is already running"}, status_code=409)

    @app.get("/api/shows", dependencies=[Depends(api_auth)])
    def api_shows(include_empty: bool = False):
        out = [ser.show(r) for r in db.q("SELECT * FROM shows ORDER BY sort, name")]
        return [s for s in out if s["item_count"] or include_empty]

    @app.get("/api/shows/{show_id}", dependencies=[Depends(api_auth)])
    def api_show(show_id: str):
        row = db.one("SELECT * FROM shows WHERE id=?", (show_id,))
        if not row:
            raise HTTPException(404, "no such show")
        return ser.show(row, with_tabs=True)

    @app.get("/api/shows/{show_id}/items", dependencies=[Depends(api_auth)])
    def api_show_items(show_id: str, tab: str | None = None, season: int | None = None, order: str = "desc",
                       page: int = 1, page_size: int = 50,
                       include_locked: bool = False):
        sql, args = item_query(show=show_id, tab=tab, order=order, include_locked=include_locked, season=season)
        return paged(sql, args, page, page_size, one=item_out)

    @app.get("/api/items", dependencies=[Depends(api_auth)])
    def api_items(q: str | None = None, show: str | None = None, tab: str | None = None, kind: str | None = None,
                  order: str = "desc", page: int = 1, page_size: int = 50, include_locked: bool = False,
                  youtube_id: str | None = None):
        sql, args = item_query(show=show, tab=tab, kind=kind, q=q, order=order, include_locked=include_locked,
                               youtube_id=youtube_id)
        return paged(sql, args, page, page_size, one=item_out)

    @app.get("/api/latest", dependencies=[Depends(api_auth)])
    def api_latest(limit: int = 20):
        sql, args = item_query()
        return [item_out(r) for r in db.q(f"{sql} LIMIT ?", args + [max(1, min(limit, 100))])]

    def get_primary(post_id: int) -> dict:
        p = ser.primary_of(post_id)
        if not p:
            raise HTTPException(404, "no such item")
        return p

    @app.get("/api/items/{post_id}", dependencies=[Depends(api_auth)])
    def api_item(post_id: int):
        return item_out(get_primary(post_id), full=True)

    @app.get("/api/items/{post_id}/comments", dependencies=[Depends(api_auth)])
    def api_comments(post_id: int, page: int = 1, page_size: int = 50):
        return ser.comments(get_primary(post_id), max(1, page), max(1, min(page_size, 200)))

    # Single posts, never merged with their other versions: the archive as the site has it
    def get_post(post_id: int) -> dict:
        p = db.post(post_id)
        if not p:
            raise HTTPException(404, "no such post")
        return p

    @app.get("/api/posts", dependencies=[Depends(api_auth)])
    def api_posts(q: str | None = None, show: str | None = None, tab: str | None = None, order: str = "desc",
                  page: int = 1, page_size: int = 50, include_locked: bool = False, youtube_id: str | None = None):
        sql, args = item_query(show=show, tab=tab, q=q, order=order, include_locked=include_locked, separate=True,
                               youtube_id=youtube_id)
        return paged(sql, args, page, page_size, ser.post_summary)

    @app.get("/api/posts/{post_id}", dependencies=[Depends(api_auth)])
    def api_post(post_id: int):
        return ser.post_full(get_post(post_id))

    @app.get("/api/posts/{post_id}/comments", dependencies=[Depends(api_auth)])
    def api_post_comments(post_id: int, page: int = 1, page_size: int = 50):
        p = get_post(post_id)
        return ser.comments(p, max(1, page), max(1, min(page_size, 200)), only_post=p["id"])

    @app.get("/api/audio/{post_id}", dependencies=[Depends(api_auth)])
    def api_audio(post_id: int):
        p = db.post(post_id)
        audio = (p or {}).get("media", {}).get("audio") if p else None
        f = safe_join(paths.archive, audio["path"]) if audio else None
        if not f or not f.exists():
            raise HTTPException(404, "no audio for this post")
        return FileResponse(f, media_type=audio.get("mime") or "audio/mp4", filename=f.name,
                            content_disposition_type="inline")

    @app.get("/api/video/{post_id}", dependencies=[Depends(api_auth)])
    def api_video(post_id: int):
        p = db.post(post_id)
        video = (p or {}).get("media", {}).get("video") if p else None
        f = safe_join(paths.media, video["path"]) if video else None
        if not f or not f.exists():
            raise HTTPException(404, "no video for this post")
        return FileResponse(f, media_type="video/mp4", filename=f.name, content_disposition_type="inline")

    # ------------------------------------------------------------------ files (contract v0.15)

    def visible_post(post_id: int) -> dict:
        p = db.post(post_id)
        if not p:
            raise HTTPException(404, "no such post")
        return p

    @app.get("/api/posts/{post_id}/files", dependencies=[Depends(api_auth)])
    def api_post_files(post_id: int):
        """Every file of one post: video, audio, documents, attachments, images, thumbnail, and Google links that
        couldn't be saved."""
        p = visible_post(post_id)
        return {"post_id": p["id"], "files": files_mod.post_files(paths, p)}

    @app.get("/api/items/{post_id}/files", dependencies=[Depends(api_auth)])
    def api_item_files(post_id: int):
        """Every file of all of an item's versions, each saying which post it's from."""
        p = visible_post(post_id)
        primary = ser.primary_of(p["id"]) or p
        return {"item_id": primary["id"], "files": files_mod.item_files(paths, ser.members(primary))}

    @app.get("/api/preview/{rel:path}", dependencies=[Depends(api_auth)])
    def api_preview(rel: str, page: int | None = None):
        """What's inside a file under /files/ (JSON), or with ?page=N one page of a PDF as a PNG."""
        f = safe_join(paths.archive, rel)
        if not f or not f.is_file():
            raise HTTPException(404, "not found")
        rel = f.relative_to(paths.archive.resolve()).as_posix()
        if page is not None:
            if files_mod.type_of(f.name, f) != "pdf":
                raise HTTPException(400, "pages are only for PDFs")
            png = files_mod.pdf_page(paths, f, page)
            if not png:
                raise HTTPException(404, "no such page")
            return FileResponse(png, media_type="image/png")
        return files_mod.preview(paths, rel, f)

    @app.get("/api/progress/{post_id}", dependencies=[Depends(api_auth)])
    def api_progress(post_id: int):
        """Where you are in the episode, in the timeline of this post's video or audio (contract v0.9)."""
        p = db.post(post_id)
        if not p:
            raise HTTPException(404, "no such post")
        return progress.get(p)

    @app.put("/api/progress/{post_id}", dependencies=[Depends(api_auth)])
    async def api_set_progress(post_id: int, request: Request):
        body = await request.json()
        p = db.post(post_id)
        if not p:
            raise HTTPException(404, "no such post")
        try:
            return progress.set(p, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/events", dependencies=[Depends(api_auth)])
    def api_events(since: str | None = None):
        if not since:
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        rows = db.q("SELECT * FROM events WHERE at > ? ORDER BY id", (since,))
        return [{"type": r["type"], "at": r["at"], "post_id": r["post_id"], "title": r["title"],
                 "message": r["message"]} for r in rows]

    @app.get("/api/problems", dependencies=[Depends(api_auth)])
    def api_problems():
        rows = db.q("SELECT id, title, problems, status FROM posts WHERE json_array_length(problems) > 0 "
                    "ORDER BY published DESC")
        return [{"post_id": r["id"], "title": r["title"], "status": r["status"], "problems": r["problems"]} for r in rows]

    @app.get("/api/feed.xml", dependencies=[Depends(api_auth)])
    def api_feed(request: Request):
        base = (db.setting("public_url") or str(request.base_url)).rstrip("/")
        token = db.setting("api_token")
        rows = db.q("SELECT * FROM posts WHERE json_extract(media,'$.audio.path') IS NOT NULL "
                    "ORDER BY published DESC, id DESC")
        logo = db.setting("site_logo")
        items = []
        for r in rows:
            a = r["media"]["audio"]
            date = r["published"] or r["first_seen"]
            try:
                pub = format_datetime(datetime.fromisoformat(iso(date)))
            except (TypeError, ValueError):
                pub = ""
            items.append(
                f"<item><title>{escape(r['title'])}</title><guid isPermaLink=\"false\">jrwi-{r['id']}</guid>"
                f"<pubDate>{pub}</pubDate><description>{escape(r['body_text'] or '')}</description>"
                f"<enclosure url=\"{escape(f'{base}/api/audio/{r['id']}?token={token}')}\" "
                f"length=\"{a.get('size') or 0}\" type=\"{escape(a.get('mime') or 'audio/mp4')}\"/>"
                + (f"<itunes:duration>{r['duration']}</itunes:duration>" if r["duration"] else "")
                + "</item>")
        image = f"<itunes:image href=\"{escape(base + files_url(logo) + '?token=' + token)}\"/>" if logo else ""
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" '
               'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
               "<title>Just Roll With It (members archive)</title>"
               f"<link>{escape(SITE_BASE)}/supporters</link><description>Private archive</description>"
               f"<itunes:block>Yes</itunes:block>{image}" + "".join(items) + "</channel></rss>")
        return Response(xml, media_type="application/rss+xml")

    @app.get("/files/{rel:path}", dependencies=[Depends(api_auth)])
    def files(rel: str):
        f = safe_join(paths.archive, rel)
        if not f or not f.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(f)

    # ------------------------------------------------------------------ login

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        if db.setting("web_password_hash"):
            return RedirectResponse("/login", 303)
        return templates.TemplateResponse(request, "login.html", {"setup": True, "error": None, "next": "/admin/settings"})

    @app.post("/setup")
    def setup_post(request: Request, password: str = Form(...), password2: str = Form(...)):
        if db.setting("web_password_hash"):
            return RedirectResponse("/login", 303)
        if len(password) < 8 or password != password2:
            return templates.TemplateResponse(request, "login.html", {
                "setup": True, "next": "/admin/settings",
                "error": "Use at least 8 characters, and type the same password twice."}, status_code=400)
        db.set_setting("web_password_hash", hash_password(password))
        request.session.update(auth=True, pw=db.setting("web_password_hash")[-12:])
        return RedirectResponse("/admin/settings", 303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if not db.setting("web_password_hash"):
            return RedirectResponse("/setup", 303)
        return templates.TemplateResponse(request, "login.html", {"setup": False, "error": None, "next": next})

    @app.post("/login")
    def login_post(request: Request, password: str = Form(...), next: str = Form("/")):
        stored = db.setting("web_password_hash") or ""
        if not check_password(password, stored):
            return templates.TemplateResponse(request, "login.html", {
                "setup": False, "error": "That password isn't right.", "next": next}, status_code=401)
        request.session.update(auth=True, pw=stored[-12:])
        return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", 303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ------------------------------------------------------------------ archive website

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def home(request: Request, q: str | None = None, page: int = 1, locked: bool = False, order: str = "desc"):
        # Like the site's members page: the feed, with the shows as tiles above it
        return feed(request, q, page, locked, order)

    @app.get("/shows/{show_id}", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def show_page(request: Request, show_id: str, tab: str | None = None, season: int | None = None, page: int = 1,
                  order: str = "desc"):
        row = db.one("SELECT * FROM shows WHERE id=?", (show_id,))
        if not row:
            raise HTTPException(404, "No such show")
        show = ser.show(row, with_tabs=True, separate=True)
        tab = tab or (show["tabs"][0]["id"] if show["tabs"] else None)
        # A tab of a show with more than one season (Episodes, Just Rolled With It) gets a page per season
        seasons = next((t.get("seasons") or [] for t in show["tabs"] if t["id"] == tab), [])
        if len(seasons) > 1:
            season = season if season in [s["season"] for s in seasons] else seasons[0]["season"]
        else:
            season = None
        sql, args = item_query(show=show_id, tab=tab, order=order, separate=True, season=season)
        data = paged(sql, args, page, 48, ser.post_summary)
        return render(request, "show.html", show=show, tab=tab, season=season, data=data, order=order,
                      pages=math.ceil(data["total"] / data["page_size"]) or 1)

    @app.get("/feed", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def feed(request: Request, q: str | None = None, page: int = 1, locked: bool = False, order: str = "desc"):
        order = "asc" if order == "asc" else "desc"
        where, args = ["status != 'gone'"], []
        if not locked:
            where.append("status != 'locked'")
        if q:
            where.append("(title LIKE ? OR body_text LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        direction = order.upper()
        rows_sql = f"SELECT * FROM posts WHERE {' AND '.join(where)} ORDER BY published {direction}, id {direction}"
        total = db.one(f"SELECT COUNT(*) AS n FROM ({rows_sql})", args)["n"]
        rows = db.q(f"{rows_sql} LIMIT 20 OFFSET ?", args + [(max(1, page) - 1) * 20])
        posts = []
        for r in rows:
            p = ser.post_full(r)
            # The site's feed shows each post's top comments, with more on request
            top = sorted_comments(r) if r["comment_count"] or r["comments_fetched"] else []
            p["top_comments"], p["more_comments"] = top[:2], len(top) > 2
            if p["audio"]:
                prog = api_progress(p["audio"]["post_id"])
                p["progress_start"] = prog.get("position") if prog and not prog.get("finished") else 0
            posts.append(p)
        shows = [ser.show(s, separate=True) for s in db.q("SELECT * FROM shows ORDER BY sort, name")]
        for s in shows:
            # Until the show's own art is saved, its newest thumbnail stands in
            s["tile"] = s["image"]
        return render(request, "feed.html", posts=posts, q=q or "", page=max(1, page),
                      pages=math.ceil(total / 20) or 1, locked=locked, order=order,
                      tiles=[s for s in shows if s["item_count"]],
                      resume=continue_watching() if page <= 1 and not q else [],
                      empty=not total and not q and not db.one("SELECT 1 FROM posts LIMIT 1"))

    CONTINUE_MIN = 10.0   # seconds in before an episode counts as started

    def continue_watching(limit: int = 12) -> list[dict]:
        """Episodes (video or audio) that were started and not finished, most recently played first. Each opens the
        version that was played last, which resumes where it was left."""
        out, seen = [], set()
        for row in db.q("SELECT * FROM progress WHERE NOT finished AND position >= ? ORDER BY updated_at DESC LIMIT 200",
                        (CONTINUE_MIN,)):
            primary = ser.primary_of(row["post_id"])
            if not primary or primary["id"] in seen or primary["status"] == "locked":
                continue
            seen.add(primary["id"])
            item = ser.summary(primary)
            played = "audio" if row["timeline"] == "audio" else "video"
            part = item[played] or item["video"] or item["audio"]
            if not part:
                continue
            at = progress.get(db.post(part["post_id"]))
            duration = at.get("duration") or part.get("duration")
            if not duration or at["position"] >= duration:
                continue
            show = db.one("SELECT name FROM shows WHERE id=?", (item["show"],))
            out.append({**item, "played": played if item[played] else ("video" if item["video"] else "audio"),
                        "href": f"/items/{part['post_id']}", "position": at["position"], "length": duration,
                        "fraction": max(0.0, min(1.0, at["position"] / duration)), "left": duration - at["position"],
                        "show_name": show["name"] if show else "", "played_at": row["updated_at"]})
            if len(out) >= limit:
                break
        return out

    def sorted_comments(post: dict, csort: str = "top") -> list[dict]:
        """A post's own comments. 'top' (most relevant, as on the site): pinned first, then the most liked.
        'new': pinned first, then newest first."""
        items = ser.comments(post, 1, 100000, only_post=post["id"])["items"]
        if csort == "new":
            return items  # already pinned first, then newest first
        return sorted(items, key=lambda c: (not c["pinned"], -(c["like_count"] or 0)))

    @app.get("/items/{post_id}/comments-html", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def item_comments_html(request: Request, post_id: int, csort: str = "top", offset: int = 0, limit: int = 10):
        """More comments for a post in the feed, as HTML. X-More says whether there are more after these."""
        post = db.post(post_id)
        if not post:
            raise HTTPException(404, "No such post")
        items = sorted_comments(post, "new" if csort == "new" else "top")
        offset, limit = max(0, offset), max(1, min(limit, 50))
        resp = templates.TemplateResponse(request, "comments_part.html", {"items": items[offset:offset + limit]})
        resp.headers["X-More"] = "1" if len(items) > offset + limit else "0"
        return resp

    @app.get("/items/{post_id}", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def item_page(request: Request, post_id: int, csort: str = "top"):
        post = db.post(post_id)
        if not post:
            raise HTTPException(404, "No such post")
        item = ser.post_full(post)
        csort = "new" if csort == "new" else "top"
        comments = {"items": sorted_comments(post, csort)}
        show = db.one("SELECT id, name FROM shows WHERE id=?", (item["show"],))
        # Every show and tab the post is in, as the site lists it: its own show first
        show_names = {r["id"]: r["name"] for r in db.q("SELECT id, name FROM shows")}
        places = [{"show": pl["show"], "name": show_names.get(pl["show"], pl["show"]), "tab": pl["tab"]}
                  for pl in item.get("places") or [{"show": item["show"], "tab": t} for t in item.get("tabs") or [item["tab"]]]]
        audio_at = api_progress(item["audio"]["post_id"]) if item["audio"] else None
        video_at = api_progress(item["video"]["post_id"]) if item["video"] and item["video"].get("url") else None
        # The side list, like the site's "Recently uploaded": newest posts from the same show
        sql, args = item_query(show=item["show"], separate=True)
        more = [ser.post_summary(r) for r in db.q(f"SELECT * FROM ({sql}) WHERE id != ? LIMIT 8", args + [post["id"]])]
        return render(request, "item.html", item=item, comments=comments["items"], show=show, places=places,
                      progress=audio_at,
                      video_progress=video_at,
                      more=more, csort=csort)

    @app.get("/items/{post_id}/files", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def item_files_page(request: Request, post_id: int, f: str = ""):
        """The file inspector: every file of a post, and of its other versions, each opened in place."""
        post = db.post(post_id)
        if not post:
            raise HTTPException(404, "No such post")
        own = files_mod.post_files(paths, post)
        primary = ser.primary_of(post_id) or post
        others = [e for e in files_mod.item_files(paths, ser.members(primary)) if e["post_id"] != post_id]
        titles = {m["id"]: m["title"] for m in ser.members(primary)}
        return render(request, "files.html", post=post, own=own, others=others, titles=titles, pick=f)

    @app.get("/admin/files", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def admin_files(request: Request):
        posts = db.q("SELECT id, media, embeds, handover, documents, attachments, images, thumbnail, links "
                     "FROM posts WHERE status IN ('complete', 'partial', 'gone')")
        return render(request, "admin_files.html", rows=files_mod.summary(paths, posts),
                      previewable=files_mod.PREVIEWABLE)

    @app.get("/pages", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def pages_index(request: Request):
        return render(request, "pages.html", pages=db.q("SELECT path, title, fetched FROM pages ORDER BY path"))

    @app.get("/page/{path:path}", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def page_view(request: Request, path: str):
        row = db.one("SELECT * FROM pages WHERE path=?", ("/" + path.strip("/"),))
        if not row:
            raise HTTPException(404, "That page hasn't been saved")
        return render(request, "page.html", page=row, html=clean_html(row["main_html"]))

    # ------------------------------------------------------------------ admin

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def admin(request: Request):
        runs = db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 15")
        events = db.q("SELECT * FROM events ORDER BY id DESC LIMIT 20")
        rows = db.q("SELECT id, title, handover FROM posts WHERE handover IS NOT NULL ORDER BY published DESC, id DESC")
        counts: dict = {}
        for r in rows:
            counts[r["handover"].get("status")] = counts.get(r["handover"].get("status"), 0) + 1
        handovers = {"rows": rows, "counts": counts,
                     "freed": sum((r["handover"].get("freed") or 0) for r in rows if r["handover"].get("status") == "done")}
        ta_set = TubeArchivist.from_settings(db.settings()).configured
        settings = db.settings()
        return render(request, "admin.html", st=status_obj(), runs=runs, events=events, handovers=handovers, ta_set=ta_set,
                      every=settings["check_interval_hours"], back_days=settings["recheck_days"],
                      comment_days=settings["comment_refresh_days"],
                      last_reparse=db.setting("last_reparse"),
                      configured=bool(db.setting("site_email") or os.environ.get("JRWI_EMAIL")))

    @app.post("/admin/check", dependencies=[Depends(web_auth)])
    def admin_check(full: str = Form("")):
        scheduler.check_now(full=bool(full))
        return RedirectResponse("/admin", 303)

    @app.post("/admin/stop", dependencies=[Depends(web_auth)])
    def admin_stop():
        scheduler.stop()
        return RedirectResponse("/admin", 303)

    @app.get("/admin/problems", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def admin_problems(request: Request):
        return render(request, "problems.html", rows=api_problems())

    @app.get("/admin/settings", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def admin_settings(request: Request, saved: bool = False, msg: str | None = None):
        return render(request, "settings.html", s=db.settings(), saved=saved, msg=msg, ta_status=db.setting("ta_status"),
                      env_login=bool(os.environ.get("JRWI_EMAIL")),
                      has_session=any(True for _ in archiver.client.session.cookies))

    @app.post("/admin/settings", dependencies=[Depends(web_auth)])
    async def admin_settings_post(request: Request):
        form = await request.form()
        s = db.settings()
        for key in ("site_email", "notify_url", "public_url", "ta_url"):
            db.set_setting(key, (form.get(key) or "").strip())
        if form.get("ta_token"):
            db.set_setting("ta_token", form["ta_token"].strip())
        elif form.get("ta_token_clear"):
            db.set_setting("ta_token", "")
        if form.get("site_password"):
            db.set_setting("site_password", form["site_password"])
            archiver.client.clear_cookies()
        for key, cast in (("check_interval_hours", float), ("max_height", int), ("comment_refresh_days", int),
                          ("request_delay", float), ("recheck_days", int)):
            try:
                db.set_setting(key, cast(form.get(key) or s[key]))
            except ValueError:
                pass
        for key in ("video", "audio", "images", "attachments", "comments", "google_docs", "public_pages",
                    "youtube_embeds", "handover"):
            db.set_setting(key, form.get(key) == "on")
        if form.get("new_web_password"):
            if len(form["new_web_password"]) >= 8:
                db.set_setting("web_password_hash", hash_password(form["new_web_password"]))
                request.session.update(auth=True, pw=db.setting("web_password_hash")[-12:])
        if form.get("regenerate_token"):
            db.set_setting("api_token", new_token())
        return RedirectResponse("/admin/settings?saved=1", 303)

    @app.post("/admin/test-ta", dependencies=[Depends(web_auth)])
    def admin_test_ta():
        ta = TubeArchivist.from_settings(db.settings())
        if not ta.configured:
            msg = "Enter TubeArchivist's address and API token, and save, first."
        else:
            try:
                msg = "TubeArchivist: " + ta.ping() + "."
                db.set_setting("ta_status", {"ok": True, "message": "reachable", "at": now()})
            except TaError as e:
                msg = f"TubeArchivist: {e}."
                db.set_setting("ta_status", {"ok": False, "message": str(e), "at": now()})
        return RedirectResponse("/admin/settings?msg=" + quote(msg), 303)

    def check_site_login() -> tuple[bool, str]:
        """Ask the members feed whether the saved session is logged in."""
        from ..site.client import SiteError, logged_in
        from ..site.parse import soup_of
        try:
            _, text = archiver.client.page(archiver.client.abs("/supporters"))
        except SiteError as e:
            return False, f"Couldn't reach jrwishow.com: {e}"
        if logged_in(soup_of(text)):
            return True, "Logged in to jrwishow.com."
        return False, "jrwishow.com shows the login button, so this login doesn't work."

    @app.post("/admin/test-login", dependencies=[Depends(web_auth)])
    def admin_test_login():
        from ..site.client import SiteError
        if archiver.status.running:
            return RedirectResponse("/admin/settings?msg=" + quote("A check is running; try again when it's done."), 303)
        s = db.settings()
        email = os.environ.get("JRWI_EMAIL") or s["site_email"]
        password = os.environ.get("JRWI_PASSWORD") or s["site_password"]
        try:
            archiver.client.login(email, password, debug_file=paths.data / "login-debug.txt")
            msg = "Login worked."
        except SiteError as e:
            msg = f"Login failed: {e} (the login details page has more)."
        return RedirectResponse("/admin/settings?msg=" + quote(msg), 303)

    @app.post("/admin/cookies", dependencies=[Depends(web_auth)])
    async def admin_cookies(request: Request):
        form = await request.form()
        raw = form.get("cookies_text") or ""
        upload = form.get("cookies_file")
        if upload is not None and hasattr(upload, "read"):
            data = await upload.read()
            if data:
                raw = data.decode("utf-8", errors="replace")
        if not raw.strip():
            return RedirectResponse("/admin/settings?msg=" + quote("No cookies were given."), 303)
        try:
            n = archiver.client.import_cookies(raw)
        except (ValueError, KeyError):
            n = 0
        if n == 0:
            msg = "That file has no jrwishow.com cookies. Export them while you're logged in on jrwishow.com."
        else:
            ok, detail = check_site_login()
            msg = f"Imported {n} cookies. {detail}"
        return RedirectResponse("/admin/settings?msg=" + quote(msg), 303)

    @app.get("/admin/login-details", dependencies=[Depends(web_auth)])
    def admin_login_details():
        f = paths.data / "login-debug.txt"
        if not f.exists():
            return Response("No failed login has been recorded.", media_type="text/plain")
        return Response(f.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")

    def loose_tags() -> list[dict]:
        """The site's tags that no show's tags match: shows that aren't in the Shows menu, and other topics."""
        names = show_tag_names(db)
        return [t for t in db.q("SELECT * FROM tags ORDER BY count DESC, name")
                if t["name"] and not tag_role(t["name"], names) and norm(t["name"]) not in GENERIC_TAG_TABS]

    @app.get("/admin/shows", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def admin_shows(request: Request, error: str = ""):
        rows = [{**r, **ser.show(r), "art_source": r["art_source"], "source": r["source"]}
                for r in db.q("SELECT * FROM shows ORDER BY sort, name")]
        return render(request, "admin_shows.html", rows=rows, loose=loose_tags(), error=error)

    @app.post("/admin/shows/add", dependencies=[Depends(web_auth)])
    def admin_shows_add(name: str = Form(...), aliases: str = Form("")):
        db.set_setting("not_shows", [x for x in (db.setting("not_shows") or []) if norm(x) != norm(name)])
        try:
            add_show(db, name, aliases=[a.strip() for a in aliases.split(",") if a.strip()])
        except ValueError as e:
            return RedirectResponse(f"/admin/shows?error={quote(str(e))}", 303)
        classify_all(db)
        reorganize(db, paths)
        return RedirectResponse("/admin/shows", 303)

    @app.post("/admin/shows/{show_id}/remove", dependencies=[Depends(web_auth)])
    def admin_shows_remove(show_id: str):
        """Shows added here, or made from a tag, can go; a tag's show stays gone until it's made a show again. The
        Shows menu's come back on their own."""
        row = db.one("SELECT * FROM shows WHERE id=? AND source IN ('manual', 'tags')", (show_id,))
        if row:
            if row["source"] == "tags":
                db.set_setting("not_shows", sorted(set(db.setting("not_shows") or []) | {row["name"]}))
            db.x("DELETE FROM shows WHERE id=?", (show_id,))
            classify_all(db)
            reorganize(db, paths)
        return RedirectResponse("/admin/shows", 303)

    PICTURES_PER_PAGE = 40

    @app.get("/admin/shows/{show_id}", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def admin_show(request: Request, show_id: str, page: int = 1, error: str = "", done: str = ""):
        row = db.one("SELECT * FROM shows WHERE id=?", (show_id,))
        if not row:
            raise HTTPException(404, "No such show")
        from_posts = post_pictures(db, paths.archive, show_id)
        pages = max(1, math.ceil(len(from_posts) / PICTURES_PER_PAGE))
        page = min(max(1, page), pages)
        shown = from_posts[(page - 1) * PICTURES_PER_PAGE:page * PICTURES_PER_PAGE]
        return render(request, "admin_show.html", show=row, art=files_url(row["art"]),
                      override=row["art_source"] == "override", source=ART_SOURCES.get(row["art_source"] or "", ""),
                      saved=[(r, files_url(r)) for r in saved_overrides(paths.archive, row)],
                      choices=[(c, files_url(c)) for c in art_choices(row)],
                      from_posts=[{**p, "url": files_url(p["rel"])} for p in shown], page=page, pages=pages,
                      total=len(from_posts), max_mb=MAX_PICTURE // 1024 // 1024, error=error, done=done)

    def back_to_show(show_id: str, error: str = "", done: str = "") -> RedirectResponse:
        q = f"?error={quote(error)}" if error else (f"?done={quote(done)}" if done else "")
        return RedirectResponse(f"/admin/shows/{quote(show_id)}{q}", 303)

    @app.post("/admin/shows/{show_id}/art", dependencies=[Depends(web_auth)])
    def admin_show_art(show_id: str, choice: str = Form(...)):
        """Override the show's picture with a picture in the archive or the JRWI logo, or (choice=auto) turn the
        override off."""
        try:
            choose_show_art(db, paths.archive, show_id, choice)
        except ValueError as e:
            return back_to_show(show_id, error=str(e))
        refresh_jellyfin(db, paths)
        return back_to_show(show_id, done="off" if choice == "auto" else "on")

    @app.post("/admin/shows/{show_id}/art/upload", dependencies=[Depends(web_auth)])
    async def admin_show_art_upload(show_id: str, picture: UploadFile = File(...)):
        data = await picture.read(MAX_PICTURE + 1)
        try:
            if not data:
                raise ValueError("pick a picture to upload first")
            set_art_override(db, paths.archive, show_id, data)
        except ValueError as e:
            return back_to_show(show_id, error=str(e))
        refresh_jellyfin(db, paths)
        return back_to_show(show_id, done="on")

    @app.post("/admin/shows/{show_id}/art/delete", dependencies=[Depends(web_auth)])
    def admin_show_art_delete(show_id: str, file: str = Form(...)):
        try:
            delete_saved_override(db, paths.archive, show_id, file)
        except ValueError as e:
            return back_to_show(show_id, error=str(e))
        return back_to_show(show_id, done="deleted")

    @app.post("/admin/shows", dependencies=[Depends(web_auth)])
    async def admin_shows_post(request: Request):
        form = await request.form()
        for r in db.q("SELECT id FROM shows"):
            if f"aliases_{r['id']}" in form:
                aliases = [a.strip() for a in form[f"aliases_{r['id']}"].split(",") if a.strip()]
                db.update("shows", "id", r["id"], {"aliases": aliases})
        classify_all(db)
        reorganize(db, paths)
        return RedirectResponse("/admin/shows", 303)

    @app.get("/admin/items/{post_id}", response_class=HTMLResponse, dependencies=[Depends(web_auth)])
    def admin_item(request: Request, post_id: int, queued: str = "", error: str = ""):
        p = db.post(post_id)
        if not p:
            raise HTTPException(404, "No such post")
        return render(request, "admin_item.html", p=p, shows=db.q("SELECT id, name FROM shows ORDER BY sort"),
                      tabs=TABS, queued=queued, st=status_obj(), ho=handover_of(p), error=error)

    def handover_of(p: dict) -> dict | None:
        """The hand-over to TubeArchivist of the episode this post is in: which post has (or had) the archived video,
        and where it stands."""
        primary = ser.primary_of(p["id"]) or p
        holder = next((m for m in ser.members(primary) if m.get("handover")), None)
        if not holder:
            return None
        h = holder["handover"]
        return {"post": holder, "h": h, "text": handover.STATUSES.get(h.get("status"), ""),
                "gone": bool(holder.get("site_gone")), "tolerance": handover.TOLERANCE}

    @app.post("/admin/items/{post_id}/handover", dependencies=[Depends(web_auth)])
    def admin_item_handover(post_id: int, choice: str = Form(...)):
        """keep: keep the archived video; allow: let it be handed over again; force: delete it at the next check even
        though the lengths differ; redownload: download the members-only video again (while the site still has it)
        and keep it."""
        p = db.post(post_id)
        if not p:
            raise HTTPException(404, "No such post")
        try:
            if choice == "redownload":
                if not handover.is_done(p):
                    raise ValueError("the archived video is still there")
                handover.remember(db, post_id, p["handover"], status="keep", force=None)
                db.update("posts", "id", post_id, {"refetch": "page"})
                started = scheduler.check_now()
                return RedirectResponse(f"/admin/items/{post_id}?queued={'now' if started else 'later'}", 303)
            handover.set_choice(db, post_id, choice)
        except ValueError as e:
            return RedirectResponse(f"/admin/items/{post_id}?error={quote(str(e))}", 303)
        return RedirectResponse(f"/admin/items/{post_id}", 303)

    @app.post("/admin/items/{post_id}", dependencies=[Depends(web_auth)])
    async def admin_item_post(request: Request, post_id: int):
        form = await request.form()
        fields = {"show_override": form.get("show_override") or None, "tab_override": form.get("tab_override") or None}
        ep = (form.get("episode_override") or "").strip()
        fields["episode_override"] = int(ep) if ep.isdigit() else None
        season = (form.get("season_override") or "").strip()
        fields["season_override"] = int(season) if season.isdigit() else None
        db.update("posts", "id", post_id, fields)
        classify_all(db)
        reorganize(db, paths)
        return RedirectResponse(f"/admin/items/{post_id}", 303)

    @app.post("/admin/items/{post_id}/refetch", dependencies=[Depends(web_auth)])
    def admin_item_refetch(post_id: int, what: str = Form("page")):
        """Download a post again from the site: its page and comments, or (what=files) everything, files included."""
        if not db.post(post_id):
            raise HTTPException(404, "No such post")
        db.update("posts", "id", post_id, {"refetch": "files" if what == "files" else "page"})
        started = scheduler.check_now()
        return RedirectResponse(f"/admin/items/{post_id}?queued={'now' if started else 'later'}", 303)

    @app.post("/admin/refetch-all", dependencies=[Depends(web_auth)])
    def admin_refetch_all():
        """Fetch every archived post's page and comments again. Downloaded files are kept."""
        db.x("UPDATE posts SET refetch='page' WHERE status IN ('complete', 'partial') AND refetch IS NULL")
        scheduler.check_now()
        return RedirectResponse("/admin", 303)

    @app.post("/admin/reparse", dependencies=[Depends(web_auth)])
    def admin_reparse():
        scheduler.reparse_now()
        return RedirectResponse("/admin", 303)

    return app
