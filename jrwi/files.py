"""Every file a post carries, and previews of them for the file inspector (contract v0.15).

A post's files are its video and audio, thumbnail, images, attachments, and the Google Docs and Drive files it links
to. Links to Google files that couldn't be saved (a Drive folder, say) are listed too, as not saved.

Previews show what's inside a file without another program:
  pdf       the number of pages, and each page as a PNG (made with poppler's pdftoppm, kept in data/previews)
  table     spreadsheets (.xlsx, .xlsm, .csv, .tsv): each sheet's rows
  document  Word and PowerPoint files (.docx, .pptx): headings, paragraphs and list items, slide by slide
  text      plain text, Markdown, JSON, subtitles and the like
  archive   what's in a .zip
Pictures, audio and video need no preview: they're shown or played from their own address.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

from defusedxml import ElementTree as ET

from .config import Paths

IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg"}
AUDIO = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".oga", ".flac", ".opus"}
VIDEO = {".mp4", ".m4v", ".webm", ".mov", ".mkv"}
TABLE = {".xlsx", ".xlsm", ".csv", ".tsv"}
DOCUMENT = {".docx", ".pptx"}
TEXT = {".txt", ".md", ".markdown", ".json", ".srt", ".vtt", ".log", ".xml", ".html", ".htm", ".yaml", ".yml",
        ".ini", ".cfg", ".rtf", ".tex", ".lua", ".py", ".js", ".css"}
ARCHIVE = {".zip"}
PREVIEWABLE = {"pdf", "table", "document", "text", "archive"}
MIMES = {".m4a": "audio/mp4", ".webp": "image/webp", ".avif": "image/avif", ".opus": "audio/ogg", ".md": "text/markdown",
         ".srt": "application/x-subrip", ".vtt": "text/vtt", ".mkv": "video/x-matroska",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
GOOGLE_FILE_HOSTS = ("docs.google.com", "drive.google.com", "drive.usercontent.google.com")

MAX_ROWS = 500          # rows shown per sheet
MAX_COLS = 60
MAX_TEXT = 400_000      # characters of text shown
MAX_ENTRIES = 1000      # files listed from a .zip
MAX_PARSE = 60 * 1024 * 1024   # bigger spreadsheets and documents aren't opened for a preview
PAGE_WIDTH = 1400       # pixels across a PDF page picture


# ------------------------------------------------------------------ what a file is

def sniff(f: Path) -> str | None:
    """The type of a file whose name doesn't say (Drive files sometimes come without one), from its first bytes."""
    try:
        with f.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            names = set(zipfile.ZipFile(f).namelist())
        except (zipfile.BadZipFile, OSError):
            return None
        if "word/document.xml" in names or "ppt/presentation.xml" in names:
            return "document"
        if "xl/workbook.xml" in names:
            return "table"
        return "archive"
    if head[:3] == b"\xff\xd8\xff" or head.startswith((b"\x89PNG", b"GIF8")) or head[8:12] == b"WEBP":
        return "image"
    if head[4:8] == b"ftyp":
        return "video"
    if head.startswith((b"ID3", b"OggS", b"fLaC")) or head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio"
    return None


def type_of(name: str, f: Path | None = None) -> str:
    ext = Path(name).suffix.lower()
    for kind, exts in (("image", IMAGE), ("audio", AUDIO), ("video", VIDEO), ("table", TABLE),
                       ("document", DOCUMENT), ("text", TEXT), ("archive", ARCHIVE)):
        if ext in exts:
            return kind
    if ext == ".pdf":
        return "pdf"
    return (sniff(f) if f is not None and f.is_file() else None) or "other"


def mime_of(name: str, kind: str | None = None) -> str:
    ext = Path(name).suffix.lower()
    if ext in MIMES:
        return MIMES[ext]
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    return {"pdf": "application/pdf", "image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg",
            "archive": "application/zip"}.get(kind or "", "application/octet-stream")


# ------------------------------------------------------------------ a post's files

def files_url(rel: str) -> str:
    return "/files/" + quote(rel)


def preview_url(rel: str) -> str:
    return "/api/preview/" + quote(rel)


def entry(paths: Paths, rel: str, role: str, post_id: int, name: str | None = None, source: str | None = None,
          size: int | None = None) -> dict | None:
    f = paths.archive / rel
    if not f.is_file():
        return None
    name = name or f.name
    kind = type_of(name, f)
    return {"name": name, "role": role, "type": kind, "mime": mime_of(name, kind),
            "size": size if size is not None else f.stat().st_size, "url": files_url(rel), "path": rel,
            "post_id": post_id, "source": source, "saved": True,
            "preview": preview_url(rel) if kind in PREVIEWABLE else None}


def post_files(paths: Paths, post: dict) -> list[dict]:
    """Every file of one post, in the order the inspector lists them."""
    pid = post["id"]
    out: list[dict] = []
    media = post.get("media") or {}
    v = media.get("video") or {}
    if v.get("path") and (paths.media / v["path"]).is_file():
        name = Path(v["path"]).name
        out.append({"name": name, "role": "video", "type": "video", "mime": mime_of(name, "video"),
                    "size": v.get("size"), "url": f"/api/video/{pid}", "path": None, "post_id": pid, "source": None,
                    "saved": True, "preview": None, "duration": v.get("duration")})
    for e in post.get("embeds") or []:
        if e.get("kind") == "youtube" and e.get("youtube_id"):
            out.append({"name": f"YouTube video {e['youtube_id']}", "role": "video", "type": "youtube", "mime": None,
                        "size": None, "url": None, "path": None, "post_id": pid, "source": e.get("url"),
                        "saved": False, "preview": None, "youtube_id": e["youtube_id"]})
    h = post.get("handover") or {}
    if h.get("status") == "done":
        out.append({"name": "Members-only video (deleted: TubeArchivist has it from YouTube)", "role": "video",
                    "type": "youtube", "mime": None, "size": None, "url": None, "path": None, "post_id": pid,
                    "source": h.get("youtube_url"), "saved": False, "preview": None, "youtube_id": h.get("youtube_id")})
    a = media.get("audio") or {}
    if a.get("path"):
        e = entry(paths, a["path"], "audio", pid)
        if e:
            e.update(url=f"/api/audio/{pid}", duration=a.get("duration"), mime=a.get("mime") or e["mime"])
            out.append(e)
    seen: set[str] = set()
    for d in post.get("documents") or []:
        e = entry(paths, d["url"], "document", pid, d.get("name"), d.get("source"))
        if e and d["url"] not in seen:
            seen.add(d["url"])
            out.append(e)
    for at in post.get("attachments") or []:
        e = entry(paths, at["url"], "attachment", pid, at.get("name"), None, at.get("size"))
        if e and at["url"] not in seen:
            seen.add(at["url"])
            out.append(e)
    for img in post.get("images") or []:
        e = entry(paths, img["url"], "image", pid, None, img.get("source"))
        if e and img["url"] not in seen:
            seen.add(img["url"])
            out.append(e)
    if post.get("thumbnail") and post["thumbnail"] not in seen:
        e = entry(paths, post["thumbnail"], "thumbnail", pid)
        if e:
            out.append(e)
    # Google Docs and Drive links that weren't saved (a folder, or one the site's owner didn't share)
    saved_sources = {d.get("source") for d in post.get("documents") or []}
    for link in post.get("links") or []:
        url = link.get("url") or ""
        if urlparse(url).hostname in GOOGLE_FILE_HOSTS and url not in saved_sources:
            out.append({"name": link.get("text") or url, "role": "link", "type": "link", "mime": None, "size": None,
                        "url": None, "path": None, "post_id": pid, "source": url, "saved": False, "preview": None})
    return out


def item_files(paths: Paths, members: list[dict]) -> list[dict]:
    """Every file of an item's versions, the main post's first; a file two versions share is listed once."""
    out, seen = [], set()
    for m in members:
        for e in post_files(paths, m):
            key = e["path"] or e["url"] or e["source"]
            if key and key in seen:
                continue
            seen.add(key)
            out.append(e)
    return out


# ------------------------------------------------------------------ previews

def cache_key(f: Path) -> str:
    st = f.stat()
    return hashlib.sha1(f"{f}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20]


def preview(paths: Paths, rel: str, f: Path) -> dict:
    """What the inspector shows for a file. Kept in data/previews until the file changes."""
    kind = type_of(f.name, f)
    base = {"name": f.name, "type": kind, "mime": mime_of(f.name, kind), "size": f.stat().st_size,
            "url": files_url(rel)}
    cache = paths.data / "previews" / f"{cache_key(f)}.json"
    if kind in PREVIEWABLE and cache.is_file():
        try:
            return {**base, **json.loads(cache.read_text())}
        except ValueError:
            pass
    if kind == "pdf":
        body = pdf_info(f, rel)
    elif kind == "table":
        body = table(f)
    elif kind == "document":
        body = document(f)
    elif kind == "text":
        body = text(f)
    elif kind == "archive":
        body = archive(f)
    else:
        return base
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(body))
    return {**base, **body}


def pdf_info(f: Path, rel: str) -> dict:
    pages = None
    exe = shutil.which("pdfinfo")
    if exe:
        try:
            out = subprocess.run([exe, str(f)], capture_output=True, text=True, timeout=60).stdout
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            pages = int(m.group(1)) if m else None
        except (OSError, subprocess.SubprocessError):
            pass
    return {"pages": pages, "page_url": preview_url(rel) + "?page={n}" if pages and shutil.which("pdftoppm") else None}


def pdf_page(paths: Paths, f: Path, page: int) -> Path | None:
    """One page of a PDF as a PNG, PAGE_WIDTH pixels across."""
    exe = shutil.which("pdftoppm")
    if not exe or page < 1:
        return None
    out = paths.data / "previews" / cache_key(f) / f"page-{page}.png"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([exe, "-png", "-f", str(page), "-l", str(page), "-scale-to-x", str(PAGE_WIDTH),
                        "-scale-to-y", "-1", "-singlefile", str(f), str(out.with_suffix(""))],
                       capture_output=True, timeout=120, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if out.is_file() else None


def cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if hasattr(v, "isoformat"):
        s = v.isoformat()
        return s[:10] if s.endswith("T00:00:00") else s
    return str(v)


def table(f: Path) -> dict:
    if f.stat().st_size > MAX_PARSE:
        return {"sheets": [], "note": "too big to show here; download it"}
    sheets = []
    if f.suffix.lower() in (".csv", ".tsv"):
        raw = read_text(f, MAX_TEXT * 4)[0]
        try:
            dialect = csv.Sniffer().sniff(raw[:5000]) if f.suffix.lower() == ".csv" else csv.excel_tab
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(raw), dialect))
        sheets.append(sheet(f.stem, rows))
    else:
        import openpyxl
        try:
            wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        except Exception as e:  # a damaged or unusual file: the inspector says so and offers the download
            return {"sheets": [], "note": f"can't be read ({type(e).__name__})"}
        try:
            for ws in wb.worksheets:
                rows = []
                for r in ws.iter_rows(values_only=True):
                    rows.append([cell(v) for v in r])
                    if len(rows) > MAX_ROWS:
                        break
                sheets.append(sheet(ws.title, rows))
        finally:
            wb.close()
    return {"sheets": sheets}


def sheet(name: str, rows: list[list[str]]) -> dict:
    # Leave out empty rows and columns at the end
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    width = max((max((i + 1 for i, c in enumerate(r) if c.strip()), default=0) for r in rows), default=0)
    return {"name": name, "rows": [r[:min(width, MAX_COLS)] for r in rows[:MAX_ROWS]],
            "truncated": len(rows) > MAX_ROWS or width > MAX_COLS}


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def document(f: Path) -> dict:
    if f.stat().st_size > MAX_PARSE:
        return {"blocks": [], "note": "too big to show here; download it"}
    try:
        z = zipfile.ZipFile(f)
        names = z.namelist()
        if "word/document.xml" in names:
            return {"blocks": word_blocks(z.read("word/document.xml"))}
        slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        blocks = []
        for i, n in enumerate(slides, 1):
            blocks.append({"kind": "heading", "level": 2, "text": f"Slide {i}"})
            for p in ET.fromstring(z.read(n)).iter(f"{A}p"):
                t = "".join(x.text or "" for x in p.iter(f"{A}t")).strip()
                if t:
                    blocks.append({"kind": "paragraph", "text": t})
        return {"blocks": blocks}
    except Exception as e:
        return {"blocks": [], "note": f"can't be read ({type(e).__name__})"}


def word_blocks(xml: bytes) -> list[dict]:
    body = ET.fromstring(xml).find(f"{W}body")
    blocks: list[dict] = []
    if body is None:
        return blocks
    for el in body:
        if el.tag == f"{W}p":
            blocks.extend(word_paragraph(el))
        elif el.tag == f"{W}tbl":
            for row in el.iter(f"{W}tr"):
                cells = ["".join(t.text or "" for t in c.iter(f"{W}t")).strip() for c in row.iter(f"{W}tc")]
                if any(cells):
                    blocks.append({"kind": "row", "cells": cells})
    return blocks


def word_paragraph(p) -> list[dict]:
    t = "".join((x.text or "") if x.tag == f"{W}t" else "\t" if x.tag == f"{W}tab" else "\n"
                for x in p.iter() if x.tag in (f"{W}t", f"{W}tab", f"{W}br")).strip()
    if not t:
        return []
    style = p.find(f"{W}pPr/{W}pStyle")
    name = (style.get(f"{W}val") if style is not None else "") or ""
    m = re.match(r"(?i)heading\s*(\d)|title", name)
    if m:
        return [{"kind": "heading", "level": int(m.group(1)) if m.group(1) else 1, "text": t}]
    if p.find(f"{W}pPr/{W}numPr") is not None or name.lower().startswith("list"):
        return [{"kind": "item", "text": t}]
    return [{"kind": "paragraph", "text": t}]


def read_text(f: Path, limit: int) -> tuple[str, bool]:
    with f.open("rb") as fh:
        raw = fh.read(limit + 1)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw[:limit].decode(enc), len(raw) > limit
        except UnicodeDecodeError:
            continue
    return raw[:limit].decode("latin-1"), len(raw) > limit


def text(f: Path) -> dict:
    body, cut = read_text(f, MAX_TEXT)
    if f.suffix.lower() == ".json" and not cut:
        try:
            body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    fmt = {".md": "markdown", ".markdown": "markdown", ".srt": "subtitles", ".vtt": "subtitles", ".json": "json",
           ".html": "html", ".htm": "html"}.get(f.suffix.lower(), "plain")
    return {"text": body, "format": fmt, "truncated": cut}


def archive(f: Path) -> dict:
    try:
        infos = zipfile.ZipFile(f).infolist()
    except (zipfile.BadZipFile, OSError) as e:
        return {"entries": [], "note": f"can't be read ({type(e).__name__})"}
    return {"entries": [{"name": i.filename, "size": i.file_size, "folder": i.is_dir()} for i in infos[:MAX_ENTRIES]],
            "truncated": len(infos) > MAX_ENTRIES}


# ------------------------------------------------------------------ what the archive holds

def summary(paths: Paths, posts: list[dict]) -> list[dict]:
    """Files in the archive by type and extension, for the admin page: how many, how big, and whether the inspector
    shows what's inside or only plays or shows them."""
    counts: dict[tuple[str, str], dict] = {}
    for p in posts:
        for e in post_files(paths, p):
            if not e["saved"]:
                key = (e["type"], "link" if e["type"] == "link" else "YouTube")
            else:
                key = (e["type"], Path(e["name"]).suffix.lower() or "(none)")
            c = counts.setdefault(key, {"type": key[0], "ext": key[1], "count": 0, "bytes": 0})
            c["count"] += 1
            c["bytes"] += e.get("size") or 0
    return sorted(counts.values(), key=lambda c: (-c["count"], c["type"], c["ext"]))
