"""Reading the members site's HTML.

Everything here works on HTML that has already been saved, so the archive can be re-read
with improved parsing later without downloading anything again.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

SameSite = Callable[[str], bool]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".heic"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".opus"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
FILE_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS | {
    ".pdf", ".zip", ".rar", ".7z", ".psd", ".txt", ".rtf", ".doc", ".docx", ".xls",
    ".xlsx", ".ppt", ".pptx", ".csv", ".epub", ".stl", ".obj", ".ttf", ".otf", ".json",
}
FILENAME_TEXT_RE = re.compile(
    r"^[^\n/\\]{1,200}\.(pdf|zip|rar|7z|png|jpe?g|gif|webp|psd|mp3|wav|flac|m4a|ogg|aac|"
    r"mp4|mov|mkv|txt|rtf|docx?|xlsx?|pptx?|csv|epub|stl|obj|svg|ttf|otf)$", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>()\\]+")
POST_ID_RE = re.compile(r"^post_(\d+)$")
DURATION_RE = re.compile(r"^\d{1,2}(:\d{2}){1,2}$")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})\b", re.I)
RELATIVE_RE = re.compile(r"\b(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago\b", re.I)
YT_EMBED_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)|youtu\.be/)([\w-]{11})")
GDOC_RE = re.compile(r"https?://docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]{20,})")
NOT_FILE_PATHS = (
    "/pricing", "/payments/", "/api/v1/media_catalog", "/load_more", "/sign_in", "/sign_up",
    "/sign_out", "/cart", "/products/", "/pages/", "/tags", "/comments", "/likes", "/reactions",
)
POST_PAGE_RE = re.compile(r"^/supporters/(posts|videos|audio|video_embeds|images|polls)/[^/]+/?$")
FILE_ATTR_RE = re.compile(r"(^|[-_])(download|attachment|file)s?([-_]|$)")
COMMENT_ID_RE = re.compile(r"^comment[_-]?(\d+)$", re.I)
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}


# --------------------------------------------------------------------------- small helpers

def norm_space(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def safe_filename(name: str, maxlen: int = 100, keep_ext: bool = True) -> str:
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[<>:"/\\|?*%\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "untitled"
    stem, ext = os.path.splitext(name) if keep_ext else (name, "")
    if keep_ext and (len(ext) > 6 or " " in ext):
        stem, ext = name, ""
    if len(stem) + len(ext) > maxlen:
        stem = stem[: max(1, maxlen - len(ext))].rstrip(" .")
    if stem.upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    return stem + ext


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def ext_of(url_or_path: str) -> str:
    path = urlparse(url_or_path).path if "://" in url_or_path else url_or_path
    return os.path.splitext(unquote(path))[1].lower()


def strip_tracking(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(q)))


def url_key(url: str) -> str:
    p = urlparse(url)
    return urlunparse(p._replace(query="", fragment=""))


def is_image_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("imgproxy.fourthwall.dev") or ext_of(url) in IMAGE_EXTS


def is_mux_stream(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "stream.mux.com" or host.endswith(".stream.mux.com")


def download_param_name(url: str) -> str | None:
    vals = parse_qs(urlparse(url).query).get("download")
    if vals and vals[0] and vals[0].lower() not in ("true", "1"):
        return vals[0]
    return None


def is_page_url(url: str, same_site: SameSite) -> bool:
    if not same_site(url):
        return False
    path = urlparse(url).path
    return (bool(POST_PAGE_RE.match(path)) or any(x in path for x in NOT_FILE_PATHS)
            or path.rstrip("/") in ("", "/supporters"))


def kind_from_url(url: str | None) -> str:
    if not url:
        return "unknown"
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "supporters":
        return parts[1]
    return "unknown"


def in_comments(tag, root) -> bool:
    for parent in tag.parents:
        if parent is root or parent is None:
            return False
        ident = " ".join(parent.get("class") or []) + " " + (parent.get("id") or "")
        if "comment" in ident.lower():
            return True
    return False


def soup_of(text: str, turbo: bool = False) -> BeautifulSoup:
    if turbo:
        # Turbo Stream responses wrap content in <template>, whose text BeautifulSoup hides.
        text = re.sub(r"</?template\b[^>]*>", "", text)
    return BeautifulSoup(text, "html.parser")


def parse_date_text(txt: str) -> str | None:
    m = DATE_RE.search(txt or "")
    if m:
        try:
            return date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = RELATIVE_RE.search(txt or "")
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"second": 0, "minute": 0, "hour": n / 24, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[unit]
        return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    low = (txt or "").strip().lower()
    if low.startswith("today") or low.startswith("just now"):
        return date.today().isoformat()
    if low.startswith("yesterday"):
        return (date.today() - timedelta(days=1)).isoformat()
    return None


def parse_date(el) -> tuple[str | None, str]:
    t = el.find("time", attrs={"datetime": True})
    if t:
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).isoformat(), t.get_text(strip=True)
        except ValueError:
            pass
    texts = []
    meta = el.select_one(".post__meta")
    if meta:
        texts.append(meta.get_text(" ", strip=True))
    texts.append(el.get_text(" ", strip=True)[:3000])
    for txt in texts:
        d = parse_date_text(txt)
        if d:
            m = DATE_RE.search(txt) or RELATIVE_RE.search(txt)
            return d, (m.group(0) if m else txt[:40])
    return None, ""


def to_int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.fullmatch(r"\s*([\d.,]+)\s*([kKmM]?)\s*", s)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    mult = {"k": 1000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(num * mult)


def duration_seconds(s: str | None) -> int | None:
    if not s or not DURATION_RE.match(s):
        return None
    total = 0
    for part in s.split(":"):
        total = total * 60 + int(part)
    return total


# --------------------------------------------------------------------------- feed

def find_post_frames(soup) -> list:
    frames = soup.select('turbo-frame[data-testid="Post"]')
    if not frames:
        frames = [f for f in soup.find_all("turbo-frame") if POST_ID_RE.match(f.get("id", ""))]
    return frames


def find_load_more(soup, page_url: str) -> str | None:
    a = soup.select_one('a[data-controller~="infinite-scroll"][href]')
    if a is None:
        a = soup.find("a", href=re.compile(r"/load_more\?"))
    return urljoin(page_url, a["href"]) if a else None


def post_card(frame):
    for t in frame.find_all("div"):
        if "post" in (t.get("class") or []):
            return t
    return frame


def counts(frame) -> tuple[int | None, int | None]:
    """(likes, comments) from the action buttons of a post card. Best effort."""
    likes = comments = None
    for el in frame.find_all(True):
        if in_comments(el, frame):
            continue
        ident = " ".join([*(el.get("class") or []), el.get("id") or "", el.get("data-testid") or "",
                          el.get("aria-label") or "", el.get("data-controller") or ""]).lower()
        num = to_int(el.get_text(" ", strip=True))
        if num is None:
            continue
        if likes is None and ("like" in ident or "reaction" in ident or "heart" in ident):
            likes = num
        elif comments is None and "comment" in ident:
            comments = num
    if likes is None and comments is None:
        nums = []
        for b in frame.find_all("button"):
            if in_comments(b, frame):
                continue
            n = to_int(b.get_text(" ", strip=True))
            if n is not None:
                nums.append(n)
        if nums:
            likes = nums[0]
            comments = nums[1] if len(nums) > 1 else None
    return likes, comments


def parse_card(frame, page_url: str) -> dict | None:
    m = POST_ID_RE.match(frame.get("id", ""))
    if not m:
        return None
    card = post_card(frame)
    title_el = frame.select_one(".post__title")
    title = norm_space(title_el.get_text(" ", strip=True)) if title_el else ""
    url = None
    share = frame.select_one("[data-link-share-url-value]")
    if share:
        url = strip_tracking(urljoin(page_url, share["data-link-share-url-value"]))
    if not url:
        for a in frame.find_all("a", href=True):
            href = urljoin(page_url, a["href"])
            if kind_from_url(href) not in ("unknown", "pricing", "payments", "load_more"):
                url = strip_tracking(href)
                break
    title_href = (title_el.get("href") or "") if title_el is not None and title_el.name == "a" else ""
    locked = bool(frame.select_one('a[href*="/available_tiers"]')) or "/supporters/pricing" in title_href
    published, shown = parse_date(frame)
    duration = None
    for s in frame.find_all(string=True):
        if DURATION_RE.match(s.strip()):
            duration = duration_seconds(s.strip())
            break
    likes, n_comments = counts(frame)
    return {
        "id": int(m.group(1)),
        "title": title or f"Post {m.group(1)}",
        "url": url,
        "path_kind": kind_from_url(url),
        "access": card.get("access", ""),
        "locked": locked,
        "published": published,
        "date_text": shown,
        "pinned": any(s.strip() == "Pinned" for s in frame.find_all(string=True)),
        "duration": duration,
        "tag_ids": [c[4:] for c in (frame.get("class") or []) if c.startswith("tag_")],
        "like_count": likes,
        "comment_count": n_comments,
    }


def parse_tags(soup) -> list[dict]:
    """Tag names from the feed's filter panel: [{'id': '20083', 'name': 'RIPTIDE', 'count': 277}]."""
    out: dict[str, dict] = {}
    for img in soup.find_all("img", alt=re.compile(r"'s tag thumbnail$")):
        name = img["alt"][: -len("'s tag thumbnail")]
        holder = img
        tag_id = None
        for _ in range(4):
            holder = holder.parent
            if holder is None:
                break
            for attr, val in holder.attrs.items():
                for v in (val if isinstance(val, list) else [val]):
                    v = str(v)
                    m = re.search(r"(?:^|\D)tag[_-]?(?:ids?)?[\[\]=_-]*(\d{2,})", v) or (
                        re.fullmatch(r"\d{2,}", v) if attr in ("value", "data-tag-id", "data-id") else None)
                    if m:
                        tag_id = m.group(1) if m.groups() else m.group(0)
                        break
                if tag_id:
                    break
            if tag_id:
                break
        nums = [to_int(s) for s in (holder or img.parent).find_all(string=True) if to_int(s) is not None] if holder else []
        out[tag_id or "name:" + name] = {"id": tag_id or "name:" + name, "name": name, "count": nums[-1] if nums else None}
    return list(out.values())


def parse_show_menu(soup, page_url: str) -> list[dict]:
    """The site's Shows menu: [{'name': 'Riptide', 'page_url': 'https://.../pages/riptide'}]."""
    shows: list[dict] = []
    seen = set()
    # The show links sit in a list next to a "View All" link to /supporters/videos.
    for view_all in soup.find_all("a", href=re.compile(r"/supporters/videos/?$")):
        parent = view_all.parent
        for _ in range(3):
            if parent is None:
                break
            links = [x for x in parent.find_all("a", href=True) if "/pages/" in x["href"]]
            if links:
                for x in links:
                    url = urljoin(page_url, x["href"])
                    name = norm_space(x.get_text(" ", strip=True))
                    if name and url not in seen:
                        seen.add(url)
                        shows.append({"name": name, "page_url": url})
                break
            parent = parent.parent
        if shows:
            break
    return shows


def nav_page_links(soup, page_url: str, same_site: SameSite) -> list[str]:
    """Public pages linked from the site header (/pages/...), plus the perks page."""
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"]).split("#")[0]
        p = urlparse(url).path
        if same_site(url) and (p.startswith("/pages/") or p.rstrip("/") == "/supporters/perks") and url not in out:
            out.append(url)
    return out


# --------------------------------------------------------------------------- one post

def walk_json(obj, found: list) -> None:
    if isinstance(obj, dict):
        pid = obj.get("playback_id") or obj.get("playbackId") or obj.get("mux_playback_id")
        if isinstance(pid, str) and pid:
            found.append({"playback_id": pid,
                          "token": obj.get("playback_token") or obj.get("token") or obj.get("playbackToken"),
                          "audio_only": "audio" in json.dumps(obj).lower()[:500]})
        for v in obj.values():
            walk_json(v, found)
    elif isinstance(obj, list):
        for v in obj:
            walk_json(v, found)
    elif isinstance(obj, str) and "stream.mux.com/" in obj:
        found.append({"url": obj})


def find_url_near(tag, root, page_url: str, same_site: SameSite) -> dict | None:
    def from_attrs(t) -> str | None:
        for attr, val in t.attrs.items():
            if attr in ("class", "id", "style", "data-testid"):
                continue
            for v in (val if isinstance(val, list) else [val]):
                v = (v or "").strip()
                for u in ([v] if v.startswith(("/", "http://", "https://")) else []) + URL_RE.findall(v):
                    full = urljoin(page_url, u)
                    if is_page_url(full, same_site) or is_image_url(full):
                        continue
                    if "twitter.com/intent" in full or "facebook.com/sharer" in full:
                        continue
                    return full
        return None

    form = tag.find_parent("form")
    if form is not None and form.get("action"):
        fields = [(i["name"], i.get("value", "")) for i in form.find_all("input", attrs={"name": True})]
        return {"url": urljoin(page_url, form["action"]), "method": (form.get("method") or "get").upper(),
                "fields": fields}
    candidates = [tag, *tag.find_all(True)]
    parent = tag.parent
    for _ in range(2):
        if parent is None or parent is root:
            break
        named = [t for t in parent.find_all(["a", "button"])
                 if FILENAME_TEXT_RE.match(norm_space(t.get_text(" ", strip=True)))]
        if len(named) > 1:
            break
        candidates.append(parent)
        parent = parent.parent
    for t in candidates:
        u = from_attrs(t)
        if u:
            return {"url": u, "method": "GET", "fields": []}
    return None


def looks_like_attachment(url: str, attr: str, same_site: SameSite) -> bool:
    p = urlparse(url)
    host, path = (p.hostname or "").lower(), p.path.lower()
    if host.endswith("mux.com") or is_image_url(url):
        return False
    if "twitter.com/intent" in url or "facebook.com/sharer" in url:
        return False
    if same_site(url):
        if is_page_url(url, same_site):
            return False
        return (ext_of(path) in FILE_EXTS or "/rails/active_storage/" in path
                or any(w in path for w in ("/attachments", "/download", "/files/"))
                or bool(FILE_ATTR_RE.search(attr)))
    if host == "storage.googleapis.com" or host.endswith("fourthwall.com") or host.endswith("fourthwall.dev"):
        return ext_of(path) in FILE_EXTS
    return False


def extract_media(card, page_url: str, same_site: SameSite) -> tuple[list[dict], list[str]]:
    """Everything downloadable in a post card. Returns (items, unresolved attachment names)."""
    items: list[dict] = []
    seen: set[str] = set()
    unresolved: list[str] = []

    def add(kind: str, key: str, **data) -> None:
        if key in seen:
            return
        seen.add(key)
        items.append({"kind": kind, "key": key, **data})

    tags = [t for t in card.find_all(True) if not in_comments(t, card)]

    for t in tags:
        pid = t.get("playback-id") if t.name in ("mux-player", "mux-video", "mux-audio") or t.has_attr("playback-id") else None
        if not pid:
            continue
        token = t.get("playback-token")
        if not token and t.get("tokens"):
            try:
                token = json.loads(t["tokens"]).get("playback")
            except (ValueError, AttributeError):
                pass
        add("mux", "mux:" + pid, playback_id=pid, token=token, drm=bool(t.get("drm-token")),
            audio_only=t.has_attr("audio") or t.name == "mux-audio")

    for t in tags:
        if t.get("id") == "mediaData" or (t.name == "script" and "json" in (t.get("type") or "")):
            try:
                data = json.loads(t.get_text() or "{}")
            except ValueError:
                continue
            found: list = []
            walk_json(data, found)
            for f in found:
                if f.get("playback_id"):
                    add("mux", "mux:" + f["playback_id"], playback_id=f["playback_id"], token=f.get("token"),
                        drm=False, audio_only=bool(f.get("audio_only")))
                elif f.get("url"):
                    u = f["url"]
                    add("hls" if ".m3u8" in u else "download", url_key(u), url=u, filename=download_param_name(u),
                        audio_only=False)

    for t in tags:
        if t.name != "a" or not t.get("href"):
            continue
        href = urljoin(page_url, t["href"].strip())
        if is_mux_stream(href) or (t.has_attr("download") and not is_image_url(href)):
            if ".m3u8" in href:
                add("hls", url_key(href), url=href, audio_only=False)
            else:
                add("download", url_key(href), url=href, filename=download_param_name(href))

    for t in tags:
        if t.name in ("video", "audio", "source") and t.get("src") and not t["src"].startswith("blob:"):
            src = urljoin(page_url, t["src"])
            if ".m3u8" in src:
                add("hls", url_key(src), url=src, audio_only=t.name == "audio")
            else:
                add("download", url_key(src), url=src, filename=None)

    for t in tags:
        vid = t.get("data-youtube-player-video-id-value")
        if vid:
            add("youtube", "yt:" + vid, video_id=vid, url=f"https://www.youtube.com/watch?v={vid}")
        if t.name == "iframe" and t.get("src"):
            src = urljoin(page_url, t["src"])
            m = YT_EMBED_RE.search(src)
            if m:
                add("youtube", "yt:" + m.group(1), video_id=m.group(1), url=f"https://www.youtube.com/watch?v={m.group(1)}")
            elif "vimeo.com" in src or "wistia" in src or "streamable.com" in src:
                add("embed", url_key(src), url=src)

    body = card.select_one(".post__body")
    for t in tags:
        if t.name == "a" and t.get("href"):
            href = urljoin(page_url, t["href"])
            if is_image_url(href) and urlparse(href).scheme in ("http", "https"):
                add("image", url_key(href), url=href)
    if body is not None:
        for img in body.find_all("img", src=True):
            src = urljoin(page_url, img["src"])
            if urlparse(src).scheme in ("http", "https"):
                add("image", url_key(src), url=src)
    media_box = card.select_one(".post__media")
    if media_box is not None:
        img = media_box.find("img", src=True)
        if img is not None:
            src = urljoin(page_url, img["src"])
            if urlparse(src).scheme in ("http", "https") and url_key(src) not in seen:
                add("thumbnail", "thumb:" + url_key(src), url=src)

    for t in tags:
        if t.name not in ("a", "button"):
            continue
        text = norm_space(t.get_text(" ", strip=True))
        if not FILENAME_TEXT_RE.match(text):
            continue
        found = find_url_near(t, card, page_url, same_site)
        if found:
            add("attachment", url_key(found["url"]) + "|" + text, filename=text, **found)
        else:
            unresolved.append(text)
    attachment_urls = {i["url"] for i in items if i["kind"] == "attachment"}
    for t in tags:
        for attr, val in t.attrs.items():
            for v in (val if isinstance(val, list) else [val]):
                v = (v or "").strip()
                for u in ([v] if v.startswith(("/supporters/", "/rails/active_storage/")) else []) + URL_RE.findall(v):
                    full = urljoin(page_url, u)
                    if full in attachment_urls or url_key(full) in seen:
                        continue
                    if looks_like_attachment(full, attr, same_site):
                        add("attachment", url_key(full), url=full, method="GET", fields=[], filename=None)

    for t in tags:
        if t.name == "a" and t.get("href"):
            m = GDOC_RE.search(t["href"])
            if m:
                label = t.get("title") or norm_space(t.get_text(" ", strip=True))
                if label.startswith("http") or not label:
                    label = ""
                add("gdoc", "gdoc:" + m.group(2), doc_type=m.group(1), doc_id=m.group(2), label=label, url=t["href"])
    return items, unresolved


def extract_body(card) -> tuple[str, str, list[dict]]:
    body = (card.select_one('.post__body [data-trim-target="content"]') or card.select_one(".post__body .rich-text")
            or card.select_one(".post__body") or card.select_one(".rich-text"))
    if body is None:
        return "", "", []
    body = BeautifulSoup(str(body), "html.parser")
    for b in body.find_all("button"):
        b.decompose()
    root = body.find() or body
    links = [{"text": norm_space(a.get_text(" ", strip=True)), "url": a["href"]} for a in root.find_all("a", href=True)]
    blocks = [norm_space(p.get_text(" ", strip=True))
              for p in root.find_all(["p", "li", "h1", "h2", "h3", "h4", "blockquote"])]
    text = "\n\n".join(b for b in blocks if b) or norm_space(root.get_text(" ", strip=True))
    return (root.decode_contents() if root is not body else str(body)), text, links


def find_post_scope(soup, post_id: int):
    """The element holding one post on its own page."""
    frame = soup.find("turbo-frame", id=f"post_{post_id}")
    if frame is not None:
        return frame
    candidates = [d for d in soup.find_all("div") if "post" in (d.get("class") or [])]
    return candidates[0] if candidates else None


def lazy_frames(soup, page_url: str, same_site: SameSite, skip_ids: set[str] = frozenset()) -> list[tuple[str, str]]:
    """Turbo frames that load their content separately (src=...), such as comment threads."""
    out = []
    for f in soup.find_all("turbo-frame", src=True):
        fid = f.get("id") or ""
        if fid in skip_ids:
            continue
        url = urljoin(page_url, f["src"])
        if same_site(url) and not any(x in url for x in ("/cart", "/sign_in", "/pricing", "/payments/")):
            out.append((fid, url))
    return out


def load_more_comment_links(soup, page_url: str, same_site: SameSite) -> list[str]:
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"])
        if same_site(url) and "comment" in urlparse(url).path.lower() and (
                a.get("data-turbo-stream") or "more" in a.get_text(" ", strip=True).lower()
                or "infinite-scroll" in (a.get("data-controller") or "")):
            out.append(url)
    return out


# --------------------------------------------------------------------------- comments

def _comment_nodes(soup) -> list:
    nodes = [el for el in soup.find_all(True) if COMMENT_ID_RE.match(el.get("id") or "")]
    if nodes:
        return nodes
    nodes = [el for el in soup.find_all(True) if "comment" in (el.get("data-testid") or "").lower()
             and el.get("data-testid", "").lower() in ("comment", "post.comment", "comments.comment")]
    if nodes:
        return nodes
    # Last resort: elements whose class is exactly a comment block (not comment lists, forms or buttons)
    out = []
    for el in soup.find_all(["div", "li", "article", "turbo-frame"]):
        classes = [c.lower() for c in el.get("class") or []]
        if any(c in ("comment", "comment-item", "comments__item", "comment__item") for c in classes):
            out.append(el)
    return out


def _first_text(el, selectors: list[str]) -> str:
    for sel in selectors:
        found = el.select_one(sel)
        if found is not None:
            t = norm_space(found.get_text(" ", strip=True))
            if t:
                return t
    return ""


def parse_comments(html_parts: list[str], page_url: str) -> list[dict]:
    """Comments from a post page and any separately loaded comment frames.

    The member-only markup hasn't been seen yet, so this looks for common patterns and always
    keeps each comment's raw HTML for re-reading later.
    """
    comments: list[dict] = []
    seen: set[str] = set()
    for html_text in html_parts:
        soup = soup_of(html_text, turbo=True)
        nodes = _comment_nodes(soup)
        node_ids = {id(n) for n in nodes}
        for pos, el in enumerate(nodes):
            m = COMMENT_ID_RE.match(el.get("id") or "")
            cid = m.group(1) if m else (el.get("data-comment-id") or el.get("data-id") or f"{abs(hash(str(el)[:400]))}")
            if cid in seen:
                continue
            seen.add(cid)
            parent_id = el.get("data-parent-id") or el.get("data-comment-parent-id")
            if not parent_id:
                for anc in el.parents:
                    if id(anc) in node_ids:
                        am = COMMENT_ID_RE.match(anc.get("id") or "")
                        parent_id = am.group(1) if am else None
                        break
            # Work on a copy without nested replies, so the author and text are this comment's own
            own = BeautifulSoup(str(el), "html.parser")
            own_root = own.find()
            for nested in list(own_root.find_all(True))[::-1]:
                if nested is not own_root and (COMMENT_ID_RE.match(nested.get("id") or "")
                                               or "repl" in " ".join(nested.get("class") or []).lower()):
                    nested.decompose()
            author = _first_text(own_root, ["[class*=author]", "[class*=user-name]", "[class*=username]",
                                            "[class*=name]", "strong", "b"])
            avatar_el = own_root.find("img", src=True)
            body_el = (own_root.select_one("[class*=rich-text]") or own_root.select_one("[class*=body]")
                       or own_root.select_one("[class*=content]") or own_root.select_one("[class*=text]")
                       or own_root.find("p"))
            body_html = body_el.decode_contents() if body_el is not None else ""
            body_text = norm_space(body_el.get_text(" ", strip=True)) if body_el is not None else ""
            when, shown = parse_date(own_root)
            likes = None
            for b in own_root.find_all(["button", "span", "div"]):
                ident = " ".join(b.get("class") or []).lower() + (b.get("data-testid") or "").lower()
                if "like" in ident or "reaction" in ident or "heart" in ident:
                    likes = to_int(b.get_text(" ", strip=True))
                    if likes is not None:
                        break
            comments.append({
                "id": str(cid), "parent_id": str(parent_id) if parent_id else None,
                "author_name": author, "author_avatar": urljoin(page_url, avatar_el["src"]) if avatar_el else None,
                "date": when, "date_text": shown, "body_html": body_html, "body_text": body_text,
                "like_count": likes, "position": pos, "raw": str(el)[:200_000],
            })
    return comments


# --------------------------------------------------------------------------- public pages

def main_content(soup):
    return soup.find("main") or soup.body or soup


def page_title(soup) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return norm_space(og["content"])
    return norm_space(soup.title.get_text()) if soup.title else ""


def page_images(el, page_url: str) -> list[str]:
    out = []
    for img in el.find_all("img", src=True):
        out.append(urljoin(page_url, img["src"]))
    for a in el.find_all("a", href=True):
        u = urljoin(page_url, a["href"])
        if is_image_url(u):
            out.append(u)
    for s in el.find_all(style=True):
        for m in re.finditer(r"url\(['\"]?([^'\")]+)", s["style"]):
            out.append(urljoin(page_url, m.group(1)))
    seen, res = set(), []
    for u in out:
        if urlparse(u).scheme in ("http", "https") and u not in seen:
            seen.add(u)
            res.append(u)
    return res


def og_image(soup, page_url: str) -> str | None:
    og = soup.find("meta", attrs={"property": "og:image"})
    return urljoin(page_url, og["content"]) if og and og.get("content") else None


def site_assets(soup, page_url: str) -> list[str]:
    """Logo, icons and stylesheets, for a copy that looks like the original."""
    out = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        if "icon" in rel or "stylesheet" in rel or "manifest" in rel:
            out.append(urljoin(page_url, link["href"]))
    header = soup.find("header")
    if header:
        logo = header.find("img", src=True)
        if logo:
            out.append(urljoin(page_url, logo["src"]))
    og = og_image(soup, page_url)
    if og:
        out.append(og)
    return list(dict.fromkeys(u for u in out if urlparse(u).scheme in ("http", "https")))
