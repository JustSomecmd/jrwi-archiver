#!/usr/bin/env python3
"""
JRWI members archive
====================

Saves everything your Just Roll With It membership gives you access to on
https://jrwishow.com/supporters (a Fourthwall memberships site):

  * post text (as JSON and as a readable HTML page)
  * images and file attachments (PDFs, character sheets, art, music)
  * audio episodes and members-only video
  * links to embedded YouTube videos (optionally the videos themselves)
  * Google Docs linked from posts, such as episode transcripts

Stop it whenever you like and run it again later: finished posts are skipped,
half-finished downloads resume, and new posts are picked up.

Quick start (full instructions in README.md):

    pip install -r requirements.txt
    python jrwi_archive.py                        # asks for your email + password
    python jrwi_archive.py --browser firefox      # or reuse your Firefox login
    python jrwi_archive.py --cookies cookies.txt  # or a cookies.txt export
"""

from __future__ import annotations

import argparse
import getpass
import html as htmllib
import json
import os
import random
import re
import shutil
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Some required packages are missing. Run this first:\n\n    pip install -r requirements.txt\n")

VERSION = "1.1.0"

DEFAULT_BASE = "https://jrwishow.com"
DEFAULT_MUX = "https://stream.mux.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
TURBO_ACCEPT = "text/vnd.turbo-stream.html, text/html, application/xhtml+xml"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".heic"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".opus"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
FILE_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS | {
    ".pdf", ".zip", ".rar", ".7z", ".psd", ".txt", ".rtf", ".doc", ".docx", ".xls",
    ".xlsx", ".ppt", ".pptx", ".csv", ".epub", ".stl", ".obj", ".ttf", ".otf", ".json",
}
FILENAME_TEXT_RE = re.compile(
    r"^[^\n/\\]{1,200}\.(pdf|zip|rar|7z|png|jpe?g|gif|webp|psd|mp3|wav|flac|m4a|ogg|aac|"
    r"mp4|mov|mkv|txt|rtf|docx?|xlsx?|pptx?|csv|epub|stl|obj|svg|ttf|otf)$",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s\"'<>()\\]+")
POST_ID_RE = re.compile(r"^post_(\d+)$")
DURATION_RE = re.compile(r"^\d{1,2}(:\d{2}){1,2}$")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})\b", re.I)
RELATIVE_RE = re.compile(r"\b(\d+)\s*(minute|hour|day|week)s?\s+ago\b", re.I)
YT_EMBED_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)|youtu\.be/)([\w-]{11})")
GDOC_RE = re.compile(r"https?://docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]{20,})")
# Paths on the members site that are pages or actions, never downloadable files.
NOT_FILE_PATHS = (
    "/pricing", "/payments/", "/api/v1/media_catalog", "/load_more", "/sign_in", "/sign_up",
    "/sign_out", "/cart", "/products/", "/pages/", "/tags", "/comments", "/likes", "/reactions",
)
POST_PAGE_RE = re.compile(r"^/supporters/(posts|videos|audio|video_embeds|images|polls)/[^/]+/?$")
FILE_ATTR_RE = re.compile(r"(^|[-_])(download|attachment|file)s?([-_]|$)")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
MEDIA_KINDS = ("video", "audio", "images", "attachments", "gdocs", "embeds")


# --------------------------------------------------------------------------- logging

class Log:
    def __init__(self) -> None:
        self.fh = None

    def open(self, path: Path) -> None:
        self.fh = open(path, "a", encoding="utf-8")
        self.fh.write(f"\n===== run started {datetime.now():%Y-%m-%d %H:%M:%S} (v{VERSION}) =====\n")

    def __call__(self, msg: str = "") -> None:
        print(msg, flush=True)
        if self.fh:
            self.fh.write(f"{datetime.now():%H:%M:%S} {msg}\n")
            self.fh.flush()


log = Log()


class ArchiveError(Exception):
    pass


class SessionExpired(ArchiveError):
    pass


class Blocked(ArchiveError):
    pass


class Transient(ArchiveError):
    """A download that broke off part way; worth retrying (it resumes)."""


def is_mux_stream(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "stream.mux.com" or host.endswith(".stream.mux.com")


def is_page_url(url: str, client) -> bool:
    """True for links to pages on the members site (not files)."""
    if not client.same_site(url):
        return False
    path = urlparse(url).path
    return bool(POST_PAGE_RE.match(path)) or any(x in path for x in NOT_FILE_PATHS) or path.rstrip("/") in ("", "/supporters")


# --------------------------------------------------------------------------- small helpers

def norm_space(s: str) -> str:
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


def ext_of(url_or_path: str) -> str:
    path = urlparse(url_or_path).path if "://" in url_or_path else url_or_path
    return os.path.splitext(unquote(path))[1].lower()


def strip_tracking(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(q)))


def url_key(url: str) -> str:
    """Identity of a URL without its query string (tokens and signatures change)."""
    p = urlparse(url)
    return urlunparse(p._replace(query="", fragment=""))


def local_href(path: str) -> str:
    """A relative file path made safe for an HTML link (names can contain '#', '?', spaces)."""
    return quote(path.replace(os.sep, "/"), safe="/")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def is_image_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("imgproxy.fourthwall.dev") or ext_of(url) in IMAGE_EXTS


def content_disposition_name(resp: requests.Response) -> str | None:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    return m.group(1).strip() if m else None


def download_param_name(url: str) -> str | None:
    """Mux 'Download' links carry the intended file name in ?download=..."""
    vals = parse_qs(urlparse(url).query).get("download")
    if vals and vals[0] and vals[0].lower() not in ("true", "1"):
        return vals[0]
    return None


def parse_date(card) -> tuple[str | None, str]:
    """Return (ISO date, text as shown) for a post card."""
    t = card.find("time", attrs={"datetime": True})
    if t:
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).date().isoformat(), t.get_text(strip=True)
        except ValueError:
            pass
    texts = []
    meta = card.select_one(".post__meta")
    if meta:
        texts.append(meta.get_text(" ", strip=True))
    texts.append(card.get_text(" ", strip=True)[:3000])
    for txt in texts:
        m = DATE_RE.search(txt)
        if m:
            try:
                d = date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
                return d.isoformat(), m.group(0)
            except ValueError:
                continue
        m = RELATIVE_RE.search(txt)
        if m:
            n, unit = int(m.group(1)), m.group(2).lower()
            delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n),
                     "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
            return (datetime.now(timezone.utc) - delta).date().isoformat(), m.group(0)
        low = txt.lower()
        if low.startswith("today"):
            return date.today().isoformat(), "Today"
        if low.startswith("yesterday"):
            return (date.today() - timedelta(days=1)).isoformat(), "Yesterday"
    return None, ""


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


# --------------------------------------------------------------------------- HTTP

class Client:
    def __init__(self, base: str, delay: float, user_agent: str) -> None:
        self.base = base.rstrip("/")
        self.host = (urlparse(self.base).hostname or "").lower()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
        self.delay = delay
        self._last = 0.0

    def abs(self, url: str) -> str:
        return urljoin(self.base + "/", url)

    def same_site(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == self.host or host.endswith("." + self.host)

    def _pace(self) -> None:
        if self.delay > 0:
            wait = self.delay * random.uniform(0.7, 1.3) - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)

    def request(self, method: str, url: str, *, accept: str | None = None, headers: dict | None = None,
                data=None, stream: bool = False, pace: bool = True, tries: int = 6) -> requests.Response:
        h = dict(headers or {})
        if accept:
            h["Accept"] = accept
        problem = ""
        for attempt in range(tries):
            if pace:
                self._pace()
            try:
                r = self.session.request(method, url, headers=h, data=data, stream=stream, timeout=(20, 120))
            except requests.RequestException as e:
                problem = type(e).__name__
            else:
                if r.status_code != 429 and r.status_code < 500:
                    self._last = time.monotonic()
                    return r
                problem = f"HTTP {r.status_code}"
                retry_after = r.headers.get("Retry-After", "")
                r.close()
                if retry_after.isdigit():
                    time.sleep(min(int(retry_after), 600))
                    continue
            finally:
                self._last = time.monotonic()
            wait = min(300, 5 * 2 ** attempt)
            log(f"    connection problem with {urlparse(url).hostname} ({problem}), trying again in {wait}s")
            time.sleep(wait)
        raise ArchiveError(f"gave up after {tries} tries ({problem}): {url}")

    def page(self, url: str, accept: str = HTML_ACCEPT) -> tuple[str, str]:
        """Fetch a page on the members site. Returns (final URL, text)."""
        r = self.request("GET", url, accept=accept)
        text = r.text
        if r.status_code == 403 and (r.headers.get("cf-mitigated") or "Just a moment" in text[:5000]
                                     or "challenge-platform" in text):
            raise Blocked("the site answered with a bot check instead of the page")
        if r.status_code >= 400:
            raise ArchiveError(f"HTTP {r.status_code} for {url}")
        final = r.url
        if urlparse(final).path.rstrip("/").endswith("/sign_in") and not urlparse(url).path.rstrip("/").endswith("/sign_in"):
            raise SessionExpired("the site sent us to the login page")
        return final, text


# --------------------------------------------------------------------------- authentication

def load_cookie_file(path: str, client: Client) -> int:
    """Load a Netscape cookies.txt (or a JSON cookie export) into the session."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    count = 0

    def keep(domain: str) -> bool:
        d = domain.lstrip(".").lower()
        return client.host == d or client.host.endswith("." + d) or d.endswith("." + client.host)

    if raw.lstrip().startswith("["):
        for c in json.loads(raw):
            domain = c.get("domain", "")
            if not keep(domain):
                continue
            exp = c.get("expirationDate") or c.get("expires")
            client.session.cookies.set_cookie(requests.cookies.create_cookie(
                name=c["name"], value=c["value"], domain=domain, path=c.get("path", "/"),
                secure=bool(c.get("secure")), expires=int(exp) if exp else None))
            count += 1
        return count

    for line in raw.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _sub, cpath, secure, expires, name, value = parts[:7]
        if not keep(domain):
            continue
        exp = int(expires) if expires.strip().isdigit() and int(expires) > 0 else None
        client.session.cookies.set_cookie(requests.cookies.create_cookie(
            name=name, value=value, domain=domain, path=cpath or "/",
            secure=secure.strip().upper() == "TRUE", expires=exp))
        count += 1
    return count


def load_browser_cookies(browser: str, client: Client) -> int:
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise ArchiveError("--browser needs yt-dlp. Run: pip install -r requirements.txt")
    profile = None
    if ":" in browser:
        browser, profile = browser.split(":", 1)
    jar = extract_cookies_from_browser(browser.lower(), profile)
    count = 0
    for c in jar:
        d = c.domain.lstrip(".").lower()
        if client.host == d or client.host.endswith("." + d) or d.endswith("." + client.host):
            client.session.cookies.set_cookie(c)
            count += 1
    return count


CAPTCHA_HINTS = ("cf-turnstile", "challenges.cloudflare.com/turnstile", "g-recaptcha", "recaptcha/api",
                 "hcaptcha", "data-sitekey")
BROWSER_HINT = ("To skip the login form, use your browser's login instead: log in on jrwishow.com in Firefox, "
                "then run with --browser firefox (or --cookies cookies.txt; see README).")


def page_message(soup) -> str:
    """The site's own error or notice text, such as 'Invalid Email or password.'"""
    for sel in ("[role=alert]", "[class*=flash]", "[class*=alert]", "[class*=error]", "[class*=notice]",
                "[class*=toast]", "[class*=invalid]"):
        for el in soup.select(sel):
            t = norm_space(el.get_text(" ", strip=True))
            if 3 <= len(t) <= 300:
                return t
    return ""


def describe_form(form) -> list[str]:
    lines = [f"form: action={form.get('action')!r} method={form.get('method')!r} "
             f"data-turbo={form.get('data-turbo')!r} data-controller={form.get('data-controller')!r}"]
    for el in form.find_all(["input", "select", "textarea", "button"]):
        lines.append(f"  <{el.name}> type={el.get('type')!r} name={el.get('name')!r} id={el.get('id')!r} "
                     f"autocomplete={el.get('autocomplete')!r} has_value={bool(el.get('value'))}")
    return lines


def login_with_password(client: Client, email: str, password: str, debug_file: Path | None = None) -> None:
    notes: list[str] = []
    response_text = ""

    def fail(message: str, blocked: bool = False):
        if debug_file is not None:
            scrubbed = re.sub(r"(<input\b[^>]*?\bvalue=)(\"[^\"]*\"|'[^']*')", r'\1""', response_text)
            debug_file.write_text("JRWI archiver login details (no password in here)\n\n" + "\n".join(notes)
                                  + "\n\n--- the site's answer (first 6000 characters) ---\n" + scrubbed[:6000],
                                  encoding="utf-8")
            message += f"\nDetails, without your password, are saved in: {debug_file}"
        raise (Blocked if blocked else ArchiveError)(message + "\n" + BROWSER_HINT)

    sign_in = client.abs("/supporters/sign_in")
    final, text = client.page(sign_in)
    soup = BeautifulSoup(text, "html.parser")
    meta_token = soup.find("meta", attrs={"name": "csrf-token"})
    meta_param = soup.find("meta", attrs={"name": "csrf-param"})
    notes.append(f"login page: {final}")
    notes.append(f"security token in page header: {'yes' if meta_token else 'no'}")
    notes.append(f"captcha signs: {[h for h in CAPTCHA_HINTS if h in text.lower()] or 'none'}")
    pw = soup.find("input", attrs={"type": "password"})
    form = pw.find_parent("form") if pw else None
    if form is not None:
        notes += describe_form(form)
    if pw is None:
        response_text = text
        fail("login failed: the login page has no password box this script can see.")
    if form is None:
        response_text = text
        fail("login failed: the login page builds its form with JavaScript, which this script can't fill in.")

    data: list[tuple[str, str]] = []
    email_done = False
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        hints = " ".join([name, inp.get("id") or "", inp.get("autocomplete") or ""]).lower()
        if typ == "password":
            data.append((name, password))
        elif typ == "email" or (typ == "text" and any(w in hints for w in ("email", "login", "username"))):
            data.append((name, email))
            email_done = True
        elif typ in ("checkbox", "radio"):
            if inp.has_attr("checked") or "remember" in name.lower():
                data.append((name, inp.get("value", "on")))
        elif typ in ("submit", "button", "image", "reset", "file"):
            continue
        else:
            data.append((name, inp.get("value", "")))
    if not email_done:
        first_text = form.find("input", attrs={"type": "text"}) or form.find("input", attrs={"type": None})
        if not first_text or not first_text.get("name"):
            response_text = text
            fail("login failed: couldn't find the email box on the login form.")
        data = [(n, email if n == first_text["name"] else v) for n, v in data]
        if not any(n == first_text["name"] for n, _ in data):
            data.append((first_text["name"], email))
    submit = form.find(["button", "input"], attrs={"type": "submit"})
    if submit is not None and submit.get("name"):
        data.append((submit["name"], submit.get("value", "")))
    # Browsers send the page's security token even when the form itself doesn't contain it
    token_name = (meta_param.get("content") if meta_param else None) or "authenticity_token"
    headers = {"Referer": final, "Origin": client.base}
    if meta_token is not None and meta_token.get("content"):
        headers["X-CSRF-Token"] = meta_token["content"]
        if not any(n == token_name for n, _ in data):
            data.append((token_name, meta_token["content"]))
            notes.append("added the page's security token to the form")
    notes.append("fields sent: " + ", ".join(n for n, _ in data))

    action = urljoin(final, form.get("action") or final)
    method = (form.get("method") or "post").upper()
    r = client.request(method, action, data=data, accept=TURBO_ACCEPT, headers=headers)
    response_text = r.text
    notes.append(f"sent to {action}: HTTP {r.status_code}, ended at {r.url}, {r.headers.get('Content-Type')}")
    if r.status_code == 403 and (r.headers.get("cf-mitigated") or "Just a moment" in response_text[:5000]
                                 or "challenge-platform" in response_text):
        fail("login failed: the site's bot protection stopped the login before it checked the password.", blocked=True)
    if r.status_code == 429:
        fail("login failed: the site says there were too many login attempts. Wait 15 to 30 minutes and try again.")

    # The answer to the login itself can be ambiguous, so ask the members feed whether we're logged in now
    _, feed = client.page(client.abs("/supporters"))
    if logged_in(BeautifulSoup(feed, "html.parser")):
        return
    after = BeautifulSoup(re.sub(r"</?template\b[^>]*>", "", response_text), "html.parser")
    message = page_message(after)
    notes.append(f"site message: {message!r}")
    if message:
        fail(f'login failed: the site said "{message}"')
    if r.status_code == 422 and not after.find("input", attrs={"type": "password"}):
        fail("login failed: the site rejected the login request itself (its security check failed), "
             "so it never got as far as checking the email and password.")
    fail(f"login failed: the site didn't log us in and gave no reason (HTTP {r.status_code}).")


def logged_in(soup) -> bool:
    """The header's 'Your account' link points to the login page only when logged out."""
    def is_sign_in(a) -> bool:
        return urlparse(a["href"]).path.rstrip("/").endswith("/sign_in")

    anchors = soup.find_all("a", href=True)
    for a in anchors:
        label = " ".join([a.get("aria-label") or "", a.get("title") or "", a.get_text(" ", strip=True)]).lower()
        if "your account" in label:
            return not is_sign_in(a)
    return not any(is_sign_in(a) for a in anchors)


def use_browser_login(client: Client, choice: str) -> None:
    """choice is a browser name (firefox, chrome, edge, safari, brave) or the path to a cookie export."""
    if os.path.exists(choice):
        n = load_cookie_file(choice, client)
        log(f"Loaded {n} cookies for {client.host} from {choice}")
        if n == 0:
            raise ArchiveError(f"{choice} has no cookies for {client.host}. "
                               "Export them while you're logged in on that site.")
    else:
        log(f"Reading your {choice} cookies (close the browser first if this fails)...")
        n = load_browser_cookies(choice, client)
        log(f"Found {n} cookies for {client.host}")
        if n == 0:
            raise ArchiveError(f"No {client.host} cookies in {choice}. Log in on jrwishow.com in {choice} first.")


def authenticate(client: Client, args, debug_file: Path | None = None) -> None:
    if args.cookies or args.browser:
        use_browser_login(client, args.cookies or args.browser)
        return
    email = args.email or os.environ.get("JRWI_EMAIL") or input("Email for jrwishow.com: ").strip()
    password = os.environ.get("JRWI_PASSWORD")
    if not password:
        if args.hide_password:
            password = getpass.getpass("Password (hidden while you type): ")
        else:
            password = input("Password (shown as you type): ")
    log("Logging in...")
    try:
        login_with_password(client, email, password, debug_file)
    except ArchiveError as e:
        if not sys.stdin.isatty():
            raise
        log(f"\n{e}\n")
        choice = input("Use your browser's login instead? Type firefox, chrome, edge, safari or brave, "
                       "or the path to a cookies.txt file (or press Enter to stop): ").strip().strip('"')
        if not choice:
            raise ArchiveError("stopped at the login") from None
        client.session.cookies.clear()
        use_browser_login(client, choice)


# --------------------------------------------------------------------------- parsing

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
    iso, shown = parse_date(frame)
    duration = None
    for s in frame.find_all(string=True):
        if DURATION_RE.match(s.strip()):
            duration = s.strip()
            break
    pinned = any(s.strip() == "Pinned" for s in frame.find_all(string=True))
    return {
        "id": m.group(1),
        "title": title or f"Post {m.group(1)}",
        "url": url,
        "kind": kind_from_url(url),
        "access": card.get("access", ""),
        "locked": locked,
        "date": iso,
        "date_text": shown,
        "pinned": pinned,
        "duration": duration,
        "tags": [c[4:] for c in (frame.get("class") or []) if c.startswith("tag_")],
    }


def walk_json(obj, found: list, key: str = "") -> None:
    if isinstance(obj, dict):
        pid = obj.get("playback_id") or obj.get("playbackId") or obj.get("mux_playback_id")
        if isinstance(pid, str) and pid:
            found.append({"playback_id": pid,
                          "token": obj.get("playback_token") or obj.get("token") or obj.get("playbackToken"),
                          "audio_only": "audio" in json.dumps(obj).lower()[:500]})
        for k, v in obj.items():
            walk_json(v, found, str(k))
    elif isinstance(obj, list):
        for v in obj:
            walk_json(v, found, key)
    elif isinstance(obj, str) and "stream.mux.com/" in obj:
        found.append({"url": obj})


def find_url_near(tag, root, page_url: str, client: Client) -> dict | None:
    """Find the download URL for an attachment element (a filename button or link)."""
    def from_attrs(t) -> str | None:
        for attr, val in t.attrs.items():
            if attr in ("class", "id", "style", "data-testid"):
                continue
            for v in (val if isinstance(val, list) else [val]):
                v = (v or "").strip()
                for u in ([v] if v.startswith(("/", "http://", "https://")) else []) + URL_RE.findall(v):
                    full = urljoin(page_url, u)
                    if is_page_url(full, client) or is_image_url(full):
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
        named = [t for t in parent.find_all(["a", "button"]) if FILENAME_TEXT_RE.match(norm_space(t.get_text(" ", strip=True)))]
        if len(named) > 1:
            break
        candidates.append(parent)
        parent = parent.parent
    for t in candidates:
        u = from_attrs(t)
        if u:
            return {"url": u, "method": "GET", "fields": []}
    return None


def looks_like_attachment(url: str, attr: str, client: Client) -> bool:
    p = urlparse(url)
    host, path = p.netloc.lower(), p.path.lower()
    if host.endswith("mux.com") or is_image_url(url):
        return False
    if "twitter.com/intent" in url or "facebook.com/sharer" in url:
        return False
    if client.same_site(url):
        if is_page_url(url, client):
            return False
        return (ext_of(path) in FILE_EXTS or "/rails/active_storage/" in path
                or any(w in path for w in ("/attachments", "/download", "/files/"))
                or bool(FILE_ATTR_RE.search(attr)))
    if host == "storage.googleapis.com" or host.endswith("fourthwall.com") or host.endswith("fourthwall.dev"):
        return ext_of(path) in FILE_EXTS
    return False


def extract_media(card, page_url: str, client: Client) -> tuple[list[dict], list[str]]:
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

    # Mux players (Fourthwall's audio and video player)
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
            audio_only=t.has_attr("audio") or t.name == "mux-audio",
            title=t.get("data-mux-player-title-value") or t.get("metadata-video-title") or "")

    # JSON blobs that may describe media
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
                        drm=False, audio_only=bool(f.get("audio_only")), title="")
                elif f.get("url"):
                    u = f["url"]
                    add("hls" if ".m3u8" in u else "download", url_key(u), url=u, label="", filename=download_param_name(u))

    # Direct download links ("Download audio", "Download video", <a download>)
    for t in tags:
        if t.name != "a" or not t.get("href"):
            continue
        href = urljoin(page_url, t["href"].strip())
        label = norm_space(t.get_text(" ", strip=True))
        if is_mux_stream(href) or (t.has_attr("download") and not is_image_url(href)):
            if ".m3u8" in href:
                add("hls", url_key(href), url=href, audio_only=False)
            else:
                add("download", url_key(href), url=href, label=label, filename=download_param_name(href))

    # Plain <video>/<audio>/<source> tags
    for t in tags:
        if t.name in ("video", "audio", "source") and t.get("src") and not t["src"].startswith("blob:"):
            src = urljoin(page_url, t["src"])
            if ".m3u8" in src:
                add("hls", url_key(src), url=src, audio_only=t.name == "audio")
            else:
                add("download", url_key(src), url=src, label="", filename=None)

    # Embedded players
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

    # Images: gallery links (full size) and images inside the post text
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
    has_player = any(i["kind"] in ("mux", "hls", "download", "youtube", "embed") for i in items)
    if media_box is not None and has_player:
        img = media_box.find("img", src=True)
        if img is not None:
            src = urljoin(page_url, img["src"])
            if urlparse(src).scheme in ("http", "https"):
                add("thumbnail", "thumb:" + url_key(src), url=src)

    # Attachments: elements whose text is a file name, then any other file-like URL
    for t in tags:
        if t.name not in ("a", "button"):
            continue
        text = norm_space(t.get_text(" ", strip=True))
        if not FILENAME_TEXT_RE.match(text):
            continue
        found = find_url_near(t, card, page_url, client)
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
                    if looks_like_attachment(full, attr, client):
                        add("attachment", url_key(full), url=full, method="GET", fields=[], filename=None)

    # Linked Google Docs (transcripts, rules documents)
    for t in tags:
        if t.name == "a" and t.get("href"):
            m = GDOC_RE.search(t["href"])
            if m:
                label = t.get("title") or norm_space(t.get_text(" ", strip=True))
                if label.startswith("http") or not label:
                    label = ""
                add("gdoc", "gdoc:" + m.group(2), doc_type=m.group(1), doc_id=m.group(2), label=label,
                    url=t["href"])
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
    links = [{"text": norm_space(a.get_text(" ", strip=True)), "url": a["href"]}
             for a in root.find_all("a", href=True)]
    blocks = [norm_space(p.get_text(" ", strip=True)) for p in root.find_all(["p", "li", "h1", "h2", "h3", "h4", "blockquote"])]
    text = "\n\n".join(b for b in blocks if b) or norm_space(root.get_text(" ", strip=True))
    return root.decode_contents() if root is not body else str(body), text, links


# --------------------------------------------------------------------------- downloading

class Downloader:
    def __init__(self, client: Client, args) -> None:
        self.client = client
        self.args = args
        self.ffmpeg = self._find_ffmpeg()

    @staticmethod
    def _find_ffmpeg() -> str | None:
        """'PATH' when ffmpeg is installed normally, else a bundled copy from imageio-ffmpeg, else None."""
        if shutil.which("ffmpeg"):
            return "PATH"
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    def file(self, url: str, dest_dir: Path, name: str | None, *, referer: str, method: str = "GET",
             fields=None, reject_html: bool = False, tries: int = 6) -> Path:
        for attempt in range(4):
            try:
                return self._file_once(url, dest_dir, name, referer=referer, method=method,
                                       fields=fields, reject_html=reject_html, tries=tries)
            except Transient as e:
                if attempt == 3:
                    raise ArchiveError(f"{e}; it will resume on the next run")
                log(f"      {e}; resuming in {10 * (attempt + 1)}s")
                time.sleep(10 * (attempt + 1))
        raise ArchiveError("unreachable")

    def _file_once(self, url: str, dest_dir: Path, name: str | None, *, referer: str, method: str,
                   fields, reject_html: bool, tries: int) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        headers = {"Referer": referer, "Origin": self.client.base}
        dest = part = None
        if name:
            dest = dest_dir / safe_filename(name)
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            part = dest.with_name(dest.name + ".part")
            if part.exists() and part.stat().st_size > 0 and method == "GET":
                headers["Range"] = f"bytes={part.stat().st_size}-"
        pace = self.client.same_site(url)
        r = self.client.request(method, url, headers=headers, data=fields or None, stream=True, pace=pace,
                                tries=tries)
        try:
            if r.status_code == 416 and part is not None and part.exists():
                os.replace(part, dest)
                return dest
            if r.status_code >= 400:
                raise ArchiveError(f"HTTP {r.status_code}")
            ctype = r.headers.get("Content-Type", "")
            if reject_html and "text/html" in ctype:
                raise ArchiveError("got a web page instead of a file (it may be private)")
            if dest is None:
                guess = content_disposition_name(r) or os.path.basename(unquote(urlparse(r.url).path)) or "download"
                dest = dest_dir / safe_filename(guess)
                if dest.exists() and dest.stat().st_size > 0:
                    return dest
                part = dest.with_name(dest.name + ".part")
            resumed = r.status_code == 206
            done = part.stat().st_size if (resumed and part.exists()) else 0
            length = int(r.headers.get("Content-Length") or 0)
            total = done + length if length else 0
            shown = time.monotonic()
            try:
                with open(part, "ab" if resumed else "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if total > 20 * 1024 * 1024 and time.monotonic() - shown > 2:
                            shown = time.monotonic()
                            print(f"\r      {dest.name[:50]}: {done * 100 // total}% of {human_size(total)}   ",
                                  end="", flush=True)
            except requests.RequestException as e:
                raise Transient(f"connection dropped at {human_size(done)} ({type(e).__name__})")
            finally:
                if total > 20 * 1024 * 1024:
                    print("\r" + " " * 90 + "\r", end="", flush=True)
            if total and done < total:
                raise Transient(f"download cut off at {human_size(done)} of {human_size(total)}")
            os.replace(part, dest)
            return dest
        finally:
            r.close()

    def ytdlp(self, url: str, dest_dir: Path, base: str, *, referer: str, audio_only: bool) -> Path:
        try:
            import yt_dlp
        except ImportError:
            raise ArchiveError("yt-dlp isn't installed. Run: pip install -r requirements.txt")
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = safe_filename(base, keep_ext=False)
        for existing in dest_dir.glob(glob_escape(base) + ".*"):
            if existing.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS and existing.stat().st_size > 0:
                return existing
        h = self.args.max_height
        if audio_only:
            fmt = "ba/b"
        elif h:
            fmt = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        else:
            fmt = "bv*+ba/b"

        class Quiet:
            def debug(self, msg):
                pass

            def info(self, msg):
                pass

            def warning(self, msg):
                if "ffprobe not found" not in msg:  # harmless when using the bundled ffmpeg
                    log(f"      yt-dlp: {msg}")

            def error(self, msg):
                log(f"      yt-dlp: {msg}")

        opts = {
            "outtmpl": str(dest_dir / (base + ".%(ext)s")),
            "format": fmt,
            "merge_output_format": "mp4",
            "http_headers": {"Referer": referer, "Origin": self.client.base,
                             "User-Agent": self.client.session.headers["User-Agent"]},
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
            "continuedl": True,
            "overwrites": False,
            "quiet": True,
            "noprogress": False,
            "logger": Quiet(),
        }
        if self.ffmpeg and self.ffmpeg != "PATH":
            opts["ffmpeg_location"] = self.ffmpeg
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:  # yt-dlp raises many types
            raise ArchiveError(f"video download failed: {str(e)[:300]}")
        downloads = (info or {}).get("requested_downloads") or []
        if downloads and downloads[0].get("filepath") and Path(downloads[0]["filepath"]).exists():
            return Path(downloads[0]["filepath"])
        for existing in dest_dir.glob(glob_escape(base) + ".*"):
            if existing.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS:
                return existing
        raise ArchiveError("yt-dlp finished but no file was found")


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


# --------------------------------------------------------------------------- archive state

class Store:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "_archive_state.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"version": 1, "crawl_complete": False, "posts": {}, "rss": {}}
        self.state.setdefault("posts", {})
        self.state.setdefault("rss", {})

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def post_dir(self, post: dict) -> Path:
        if not post.get("folder"):
            name = safe_filename(f"{post.get('date') or 'undated'}_{post['id']}_{post['title']}", 90, keep_ext=False)
            post["folder"] = f"posts/{name}"
        d = self.root / post["folder"]
        d.mkdir(parents=True, exist_ok=True)
        return d


# --------------------------------------------------------------------------- feed crawl

def crawl_feed(client: Client, store: Store, args) -> None:
    posts = store.state["posts"]
    incremental = store.state.get("crawl_complete") and not args.refresh
    log("Checking the members feed for new posts..." if incremental else "Reading the full members feed (this takes a while the first time)...")
    url: str | None = client.abs("/supporters")
    visited: set[str] = set()
    seen_ids: set[str] = set()
    known_streak = new = page = 0
    reached_end = False
    while url:
        if url in visited:
            log("  The feed's 'load more' link repeated itself, so the crawl stopped there.")
            break
        visited.add(url)
        page += 1
        final, text = client.page(url, accept=HTML_ACCEPT if page == 1 else TURBO_ACCEPT)
        if page > 1:
            # Turbo Stream responses wrap posts in <template>, whose text BeautifulSoup hides.
            text = re.sub(r"</?template\b[^>]*>", "", text)
        soup = BeautifulSoup(text, "html.parser")
        if page == 1 and not logged_in(soup):
            raise SessionExpired("the members feed shows the login button, so the login didn't stick")
        if args.dump_html:
            dump = store.root / "_debug"
            dump.mkdir(exist_ok=True)
            (dump / f"feed_page_{page:04d}.html").write_text(text, encoding="utf-8")
        frames = find_post_frames(soup)
        for frame in frames:
            card = parse_card(frame, final)
            if not card or card["id"] in seen_ids:
                continue
            seen_ids.add(card["id"])
            old = posts.get(card["id"])
            if old is None:
                card.update(status="locked" if card["locked"] else "new", files={}, problems=[],
                            first_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                posts[card["id"]] = card
                new += 1
                known_streak = 0
            else:
                was_locked = old.get("status") == "locked"
                for k in ("title", "url", "kind", "access", "locked", "pinned", "duration", "tags", "date_text"):
                    if card.get(k) not in (None, ""):
                        old[k] = card[k]
                if card.get("date") and not old.get("date"):
                    old["date"] = card["date"]
                if was_locked and not card["locked"]:
                    old["status"] = "new"
                elif card["locked"] and old.get("status") != "complete":
                    old["status"] = "locked"
                if not card["pinned"]:
                    known_streak += 1
        print(f"\r  feed page {page}: {len(seen_ids)} posts seen, {new} new   ", end="", flush=True)
        if page % 10 == 0:
            store.save()
        if incremental and known_streak >= 15:
            break
        nxt = find_load_more(soup, final)
        if not frames or not nxt:
            reached_end = True
            break
        url = nxt
    print()
    if reached_end:
        store.state["crawl_complete"] = True
    store.state["last_crawl"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.save()
    log(f"Feed: {len(posts)} posts known, {new} new this run.")


# --------------------------------------------------------------------------- per-post processing

def enabled(kind: str, args) -> bool:
    return {
        "video": not args.no_video, "audio": not args.no_audio, "images": not args.no_images,
        "attachments": not args.no_attachments, "gdocs": not args.no_google_docs, "embeds": args.embeds,
    }[kind]


def media_class(item: dict) -> str:
    k = item["kind"]
    if k in ("image", "thumbnail"):
        return "images"
    if k == "gdoc":
        return "gdocs"
    if k in ("youtube", "embed"):
        return "embeds"
    if k == "mux" or k == "hls":
        return "audio" if item.get("audio_only") else "video"
    if k == "download":
        e = ext_of(item.get("filename") or item["url"])
        if e in AUDIO_EXTS:
            return "audio"
        if e in VIDEO_EXTS:
            return "video"
        return "attachments"
    return "attachments"


def needs_work(post: dict, args) -> bool:
    if post.get("status") in ("locked", "gone") or not post.get("url"):
        return False
    if args.only and post.get("kind") not in args.only:
        return False
    if post.get("status") != "complete":
        return True
    return any(enabled(k, args) for k in post.get("skipped", []))


def process_post(client: Client, dl: Downloader, store: Store, post: dict, args) -> None:
    final, text = client.page(post["url"])
    if "/pricing" in urlparse(final).path or "/payments/" in urlparse(final).path:
        post["status"] = "locked"
        post["locked"] = True
        return
    soup = BeautifulSoup(text, "html.parser")
    if not logged_in(soup):
        raise SessionExpired("the site logged us out part way through")
    frame = soup.find("turbo-frame", id=f"post_{post['id']}")
    card = post_card(frame) if frame is not None else None
    if card is None:
        candidates = [d for d in soup.find_all("div") if "post" in (d.get("class") or [])]
        card = candidates[0] if candidates else None
    if card is None:
        raise ArchiveError("couldn't find the post on its page (the site layout may have changed)")
    scope = frame if frame is not None else card

    if scope.select_one('a[href*="/available_tiers"]'):
        post["status"] = "locked"
        post["locked"] = True
        return

    fresh = parse_card(frame, final) if frame is not None else None
    if fresh:
        if fresh["title"] and not fresh["title"].startswith("Post "):
            post["title"] = fresh["title"]
        for k in ("date", "date_text", "duration"):
            if fresh.get(k) and not post.get(k):
                post[k] = fresh[k]
    if not post.get("date"):
        post["date"], post["date_text"] = parse_date(scope)

    d = store.post_dir(post)
    (d / "card.html").write_text(str(scope), encoding="utf-8")
    if args.dump_html:
        (d / "page.html").write_text(text, encoding="utf-8")

    body_html, body_text, links = extract_body(card)
    items, unresolved = extract_media(scope, final, client)
    files: dict = post.setdefault("files", {})
    failures: list[str] = []
    notes: list[str] = []
    skipped: set[str] = set()
    url_to_local: dict[str, str] = {}

    download_paths = [urlparse(i["url"]).path for i in items if i["kind"] == "download"]
    direct_mux_ids = {i["playback_id"] for i in items if i["kind"] == "mux"
                      and any(f"/{i['playback_id']}/" in p or p.startswith(f"/{i['playback_id']}.") for p in download_paths)}

    title_base = safe_filename(post["title"], 90, keep_ext=False)
    used_names: set[str] = {Path(v["path"]).stem.lower() for v in files.values()
                            if v.get("type") in ("audio", "video")}

    def unique(base: str) -> str:
        name, n = base, 2
        while name.lower() in used_names:
            name = f"{base} ({n})"
            n += 1
        used_names.add(name.lower())
        return name

    def record(key: str, path: Path, cls: str, source: str | None = None) -> None:
        rel = path.relative_to(store.root).as_posix()
        files[key] = {"path": rel, "type": cls}
        if source:
            url_to_local[url_key(source)] = os.path.relpath(path, d).replace(os.sep, "/")

    image_n = 0
    for it in items:
        cls = media_class(it)
        key = it["key"]
        if not enabled(cls, args):
            skipped.add(cls)
            continue
        if key in files and (store.root / files[key]["path"]).exists():
            if it.get("url"):
                url_to_local[url_key(it["url"])] = os.path.relpath(store.root / files[key]["path"], d).replace(os.sep, "/")
            continue
        try:
            if it["kind"] == "image":
                image_n += 1
                e = ext_of(it["url"]) or ".jpg"
                p = dl.file(it["url"], d / "images", f"{image_n:02d}{e}", referer=final)
                record(key, p, "image", it["url"])
            elif it["kind"] == "thumbnail":
                e = ext_of(it["url"]) or ".jpg"
                p = dl.file(it["url"], d, f"thumbnail{e}", referer=final)
                record(key, p, "thumbnail", it["url"])
            elif it["kind"] == "download":
                name = it.get("filename") or None
                if not name:
                    e = ext_of(it["url"])
                    name = unique(title_base) + e if e else None
                log(f"    downloading {cls}: {name or it['url']}")
                p = dl.file(it["url"], d, name, referer=final)
                record(key, p, cls, it["url"])
            elif it["kind"] == "mux":
                pid = it["playback_id"]
                if pid in direct_mux_ids:
                    continue
                if it.get("drm"):
                    notes.append(f"{cls} is DRM-protected, so it was not downloaded")
                    continue
                q = f"?token={it['token']}" if it.get("token") else ""
                base = unique(title_base)
                p = None
                if it.get("audio_only"):
                    try:
                        log(f"    downloading audio: {base}.m4a")
                        p = dl.file(f"{args.mux_base}/{pid}/audio.m4a{q}", d, base + ".m4a", referer=final)
                    except ArchiveError:
                        p = None
                if p is None:
                    log(f"    downloading {cls} stream: {base}")
                    p = dl.ytdlp(f"{args.mux_base}/{pid}.m3u8{q}", d, base, referer=final,
                                 audio_only=bool(it.get("audio_only")))
                record(key, p, cls)
            elif it["kind"] == "hls":
                base = unique(title_base)
                log(f"    downloading {cls} stream: {base}")
                p = dl.ytdlp(it["url"], d, base, referer=final, audio_only=bool(it.get("audio_only")))
                record(key, p, cls)
            elif it["kind"] == "attachment":
                log(f"    downloading attachment: {it.get('filename') or it['url']}")
                p = dl.file(it["url"], d / "attachments", it.get("filename"), referer=final,
                            method=it.get("method", "GET"), fields=it.get("fields"),
                            reject_html=True)
                record(key, p, "attachment", it["url"])
            elif it["kind"] in ("youtube", "embed"):
                log(f"    downloading embedded video: {it['url']}")
                base = f"youtube {it['video_id']}" if it["kind"] == "youtube" else unique(title_base + " embed")
                p = dl.ytdlp(it["url"], d, base, referer=final, audio_only=False)
                record(key, p, "embed")
            elif it["kind"] == "gdoc":
                fmt = {"document": ("export?format=pdf", ".pdf"), "spreadsheets": ("export?format=xlsx", ".xlsx"),
                       "presentation": ("export/pdf", ".pdf")}[it["doc_type"]]
                export = f"https://docs.google.com/{it['doc_type']}/d/{it['doc_id']}/{fmt[0]}"
                label = it.get("label") or f"Google doc {it['doc_id'][:10]}"
                name = safe_filename(f"{label} [{it['doc_id'][:8]}]", 110, keep_ext=False) + fmt[1]
                try:
                    p = dl.file(export, store.root / "google-docs", name, referer=final, reject_html=True,
                                tries=2)
                    record(key, p, "gdoc", it["url"])
                except ArchiveError as e:
                    if "HTTP 4" in str(e) or "web page instead" in str(e):
                        notes.append(f"linked Google Doc not saved ({e}): {it['url']}")
                    else:
                        raise
        except ArchiveError as e:
            if it["kind"] == "attachment" and "web page instead of a file" in str(e):
                notes.append(f"attachment '{it.get('filename') or it['url']}' could not be downloaded ({e})")
            else:
                failures.append(f"{cls}: {e}")
            log(f"    ! {cls} failed: {e}")

    for name in unresolved:
        notes.append(f"attachment '{name}' is listed but no download link was found in the page")

    embeds = [{"kind": i["kind"], "url": i["url"]} for i in items if i["kind"] in ("youtube", "embed")]
    meta = {
        "id": post["id"], "title": post["title"], "date": post.get("date"), "date_shown": post.get("date_text"),
        "type": post.get("kind"), "url": post["url"], "access": post.get("access"), "pinned": post.get("pinned"),
        "duration": post.get("duration"), "tags": post.get("tags"), "body_text": body_text, "body_html": body_html,
        "links": links, "embedded_videos": embeds,
        "files": {k: v["path"] for k, v in files.items()},
        "notes": notes, "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (d / "post.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    write_post_html(d, store, post, body_html, files, links, embeds, url_to_local, notes)

    post["problems"] = failures + notes
    post["skipped"] = sorted(skipped)
    post["status"] = "partial" if failures else "complete"


# --------------------------------------------------------------------------- readable HTML

PAGE_CSS = """
*{box-sizing:border-box} body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;
line-height:1.55;color:#1d1d1f;background:#fafafa}
a{color:#0b5cad} h1{line-height:1.2;margin-bottom:.3rem} .meta{color:#666;margin-bottom:1.5rem}
video,img{max-width:100%;border-radius:6px} audio{width:100%} .gallery img{margin:.4rem 0}
.box{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:1rem 1.2rem;margin:1rem 0}
.notes{color:#8a5a00} table{border-collapse:collapse;width:100%} td,th{padding:.35rem .5rem;border-bottom:1px solid #e5e5e5;
text-align:left;vertical-align:top} th{background:#f0f0f0;position:sticky;top:0} input{padding:.5rem;width:100%;
margin:.5rem 0 1rem;font-size:1rem} .muted{color:#999}
"""


def write_post_html(d: Path, store: Store, post: dict, body_html: str, files: dict, links: list,
                    embeds: list, url_to_local: dict, notes: list) -> None:
    e = htmllib.escape

    def rel(path: str) -> str:
        return local_href(os.path.relpath(store.root / path, d))

    body = BeautifulSoup(body_html or "", "html.parser")
    for tag in body.find_all(["img", "a"]):
        attr = "src" if tag.name == "img" else "href"
        if tag.get(attr) and url_key(tag[attr]) in url_to_local:
            tag[attr] = local_href(url_to_local[url_key(tag[attr])])
    out = [f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{e(post['title'])}</title>",
           f"<style>{PAGE_CSS}</style></head><body>",
           "<p><a href='../../index.html'>&larr; All posts</a></p>",
           f"<h1>{e(post['title'])}</h1>",
           f"<div class='meta'>{e(post.get('date_text') or post.get('date') or '')} &middot; {e(post.get('kind') or '')}"
           f" &middot; <a href='{e(post['url'])}'>original post</a></div>"]
    by_type: dict[str, list[str]] = {}
    for info in files.values():
        by_type.setdefault(info["type"], []).append(info["path"])
    thumbs = by_type.get("thumbnail", [])
    poster = f" poster='{e(rel(thumbs[0]))}'" if thumbs else ""
    for p in by_type.get("video", []) + by_type.get("embed", []):
        out.append(f"<div class='box'><video controls preload='metadata'{poster} src='{e(rel(p))}'></video>"
                   f"<div><a href='{e(rel(p))}'>{e(Path(p).name)}</a></div></div>")
    for p in by_type.get("audio", []):
        out.append(f"<div class='box'><audio controls preload='none' src='{e(rel(p))}'></audio>"
                   f"<div><a href='{e(rel(p))}'>{e(Path(p).name)}</a></div></div>")
    if body.get_text(strip=True) or body.find("img"):
        out.append(f"<div class='box'>{body}</div>")
    images = by_type.get("image", [])
    if images:
        out.append("<div class='gallery'>" + "".join(
            f"<a href='{e(rel(p))}'><img loading='lazy' src='{e(rel(p))}'></a>" for p in sorted(images)) + "</div>")
    extra = by_type.get("attachment", []) + by_type.get("gdoc", [])
    if extra:
        out.append("<div class='box'><b>Files</b><ul>" + "".join(
            f"<li><a href='{e(rel(p))}'>{e(Path(p).name)}</a></li>" for p in extra) + "</ul></div>")
    online = [x for x in embeds if not any(i["type"] == "embed" for i in files.values())]
    if online:
        out.append("<div class='box'><b>Embedded videos (online)</b><ul>" + "".join(
            f"<li><a href='{e(x['url'])}'>{e(x['url'])}</a></li>" for x in online) + "</ul></div>")
    if notes:
        out.append("<div class='box notes'><b>Notes</b><ul>" + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul></div>")
    out.append("</body></html>")
    (d / "post.html").write_text("\n".join(out), encoding="utf-8")


def write_index(store: Store) -> None:
    e = htmllib.escape
    posts = sorted(store.state["posts"].values(), key=lambda p: (p.get("date") or "", int(p["id"])), reverse=True)
    rows = []
    for p in posts:
        folder = p.get("folder")
        if folder and (store.root / folder / "post.html").exists():
            link = f"<a href='{e(local_href(folder))}/post.html'>{e(p['title'])}</a>"
        elif p.get("url"):
            link = f"<a class='muted' href='{e(p['url'])}'>{e(p['title'])}</a>"
        else:
            link = e(p["title"])
        status = {"complete": "", "locked": "not in your tier", "partial": "incomplete",
                  "error": "error", "new": "not yet archived", "gone": "removed from the site"}.get(p.get("status", ""), p.get("status", ""))
        rows.append(f"<tr><td>{e(p.get('date') or '')}</td><td>{link}</td><td>{e(p.get('kind') or '')}</td>"
                    f"<td class='muted'>{e(status)}</td></tr>")
    done = sum(1 for p in posts if p.get("status") == "complete")
    html_doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Just Roll With It: members archive</title>"
        f"<style>{PAGE_CSS} body{{max-width:1100px}}</style></head><body>"
        f"<h1>Just Roll With It: members archive</h1><div class='meta'>{done} of {len(posts)} posts archived"
        f" &middot; updated {datetime.now():%Y-%m-%d %H:%M}</div>"
        "<input id='q' placeholder='Filter by title, date or type...' oninput='f()'>"
        "<table id='t'><tr><th>Date</th><th>Title</th><th>Type</th><th></th></tr>" + "".join(rows) + "</table>"
        "<script>function f(){var q=document.getElementById('q').value.toLowerCase();"
        "document.querySelectorAll('#t tr').forEach(function(r,i){if(i)r.style.display="
        "r.textContent.toLowerCase().indexOf(q)>-1?'':'none'})}</script></body></html>")
    (store.root / "index.html").write_text(html_doc, encoding="utf-8")


def write_problems(store: Store) -> int:
    lines = []
    for p in sorted(store.state["posts"].values(), key=lambda x: x.get("date") or ""):
        if p.get("problems"):
            lines.append(f"{p.get('date')}  {p['title']}\n  {p.get('url')}")
            lines += [f"  - {x}" for x in p["problems"]]
            lines.append("")
    path = store.root / "problems.txt"
    if lines:
        path.write_text("\n".join(lines), encoding="utf-8")
    elif path.exists():
        path.unlink()
    return sum(1 for p in store.state["posts"].values() if p.get("problems"))


# --------------------------------------------------------------------------- extras: perks page and RSS

def archive_perks(client: Client, dl: Downloader, store: Store) -> None:
    try:
        final, text = client.page(client.abs("/supporters/perks"))
    except ArchiveError as e:
        log(f"Perks page skipped: {e}")
        return
    d = store.root / "perks"
    d.mkdir(exist_ok=True)
    (d / "perks.html").write_text(text, encoding="utf-8")
    soup = BeautifulSoup(text, "html.parser")
    main = soup.find("main") or soup
    links = sorted({(norm_space(a.get_text(" ", strip=True)), urljoin(final, a["href"])) for a in main.find_all("a", href=True)})
    (d / "links.txt").write_text("\n".join(f"{t}\t{u}" for t, u in links), encoding="utf-8")
    feeds = [u for _, u in links if re.search(r"rss|feed|\.xml|podcast", u, re.I)]
    if feeds:
        log("Possible private podcast feed links on your perks page (use with --rss):")
        for u in feeds:
            log(f"  {u}")
    items, _ = extract_media(main, final, client)
    for it in items:
        if it["kind"] == "attachment":
            try:
                dl.file(it["url"], d, it.get("filename"), referer=final, method=it.get("method", "GET"),
                        fields=it.get("fields"), reject_html=True)
            except ArchiveError as e:
                log(f"  perks file failed: {e}")


def archive_rss(client: Client, dl: Downloader, store: Store, url: str) -> None:
    log("Downloading your private podcast feed...")
    r = client.request("GET", url, accept="application/rss+xml, application/xml, text/xml, */*", pace=False)
    if r.status_code >= 400:
        log(f"  feed failed: HTTP {r.status_code}")
        return
    d = store.root / "rss"
    d.mkdir(exist_ok=True)
    (d / "feed.xml").write_bytes(r.content)
    try:
        items = ET.fromstring(r.content).findall("./channel/item")
    except ET.ParseError as e:
        log(f"  that URL isn't a valid RSS feed ({e})")
        return
    done_map = store.state["rss"]
    for n, item in enumerate(items, 1):
        enc = item.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        eurl = enc.get("url")
        guid = item.findtext("guid") or url_key(eurl)
        if guid in done_map and (d / done_map[guid]).exists():
            continue
        title = item.findtext("title") or "episode"
        try:
            day = parsedate_to_datetime(item.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            day = "undated"
        e = ext_of(eurl) or {"audio/mpeg": ".mp3", "audio/x-m4a": ".m4a", "audio/mp4": ".m4a",
                             "video/mp4": ".mp4"}.get(enc.get("type", ""), ".mp3")
        name = safe_filename(f"{day} {title}", 110, keep_ext=False) + e
        log(f"  [{n}/{len(items)}] {name}")
        try:
            p = dl.file(eurl, d, name, referer=client.base)
            done_map[guid] = p.name
            store.save()
        except ArchiveError as err:
            log(f"    ! failed: {err}")


# --------------------------------------------------------------------------- main

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Archive the Just Roll With It members area (jrwishow.com/supporters) to your computer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run it again any time to resume or to pick up new posts.")
    ap.add_argument("--out", default="JRWI Archive", help="folder to save into (default: 'JRWI Archive')")
    auth = ap.add_argument_group("login (pick one; default asks for email and password)")
    auth.add_argument("--email", help="your jrwishow.com email (you'll be asked for the password)")
    auth.add_argument("--browser", help="reuse the login from this browser: firefox, chrome, edge, safari, brave...")
    auth.add_argument("--cookies", help="path to a cookies.txt export from your browser")
    auth.add_argument("--hide-password", action="store_true", help="hide the password while you type it")
    what = ap.add_argument_group("what to save")
    what.add_argument("--no-video", action="store_true", help="skip members-only video (the bulk of the disk space)")
    what.add_argument("--no-audio", action="store_true", help="skip audio episodes")
    what.add_argument("--no-images", action="store_true", help="skip images")
    what.add_argument("--no-attachments", action="store_true", help="skip PDFs and other attached files")
    what.add_argument("--no-google-docs", action="store_true", help="don't save linked Google Docs (transcripts)")
    what.add_argument("--embeds", action="store_true", help="also download YouTube videos embedded in posts")
    what.add_argument("--max-height", type=int, metavar="PIXELS",
                      help="limit video quality, e.g. 720 (default: best available)")
    what.add_argument("--only", help="only these post types, comma separated: videos,audio,posts,video_embeds")
    what.add_argument("--rss", metavar="URL", help="also download every episode in your private podcast RSS feed")
    run = ap.add_argument_group("running")
    run.add_argument("--limit", type=int, help="stop after this many posts (handy for a first test)")
    run.add_argument("--refresh", action="store_true", help="re-read the whole feed (use after changing tier)")
    run.add_argument("--retry-failed", action="store_true", help="only retry posts that had errors")
    run.add_argument("--delay", type=float, default=1.5, help="seconds between page requests (default 1.5)")
    run.add_argument("--user-agent", default=DEFAULT_UA, help=argparse.SUPPRESS)
    run.add_argument("--dump-html", action="store_true", help="save raw pages into the archive for troubleshooting")
    run.add_argument("--base-url", default=DEFAULT_BASE, help=argparse.SUPPRESS)
    run.add_argument("--mux-base", default=DEFAULT_MUX, help=argparse.SUPPRESS)
    run.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    if sum(bool(x) for x in (args.browser, args.cookies)) > 1:
        ap.error("use either --browser or --cookies, not both")
    args.only = {x.strip() for x in args.only.split(",")} if args.only else None
    args.mux_base = args.mux_base.rstrip("/")
    return args


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    store = Store(args.out)
    log.open(store.root / "archive.log")
    log(f"JRWI archiver {VERSION}: saving to {store.root}")
    client = Client(args.base_url, args.delay, args.user_agent)
    dl = Downloader(client, args)
    if not dl.ffmpeg and not args.no_video:
        log("Note: ffmpeg wasn't found. Videos will still download, but install ffmpeg for the best files "
            "(see README).")

    try:
        authenticate(client, args, store.root / "login-debug.txt")
        crawl_feed(client, store, args)

        posts = store.state["posts"]
        todo = [p for p in posts.values() if needs_work(p, args)]
        if args.retry_failed:
            todo = [p for p in todo if p.get("status") in ("partial", "error")]
        todo.sort(key=lambda p: (p.get("date") or "", int(p["id"])), reverse=True)
        if args.limit:
            todo = todo[: args.limit]
        locked = sum(1 for p in posts.values() if p.get("status") == "locked")
        log(f"{len(todo)} post(s) to archive this run. {locked} post(s) are above your membership tier "
            "and will be skipped.")

        for n, post in enumerate(todo, 1):
            log(f"[{n}/{len(todo)}] {post.get('date') or '????-??-??'}  {post['title']}  ({post.get('kind')})")
            try:
                process_post(client, dl, store, post, args)
            except (SessionExpired, Blocked):
                raise
            except ArchiveError as e:
                post["status"] = "gone" if str(e).startswith("HTTP 404") else "error"
                post["problems"] = [str(e)]
                log(f"    ! {e}")
            except Exception as e:  # a parsing bug shouldn't stop the whole archive
                post["status"] = "error"
                post["problems"] = [f"unexpected error: {type(e).__name__}: {e}"]
                log(f"    ! unexpected error: {type(e).__name__}: {e}")
                if log.fh:
                    import traceback
                    log.fh.write(traceback.format_exc())
            store.save()
            if n % 25 == 0:
                write_index(store)

        archive_perks(client, dl, store)
        if args.rss:
            archive_rss(client, dl, store, args.rss)
    except KeyboardInterrupt:
        print()
        log("Stopped. Everything finished so far is saved; run the same command again to continue.")
        store.save()
        write_index(store)
        return 130
    except SessionExpired as e:
        store.save()
        write_index(store)
        log(f"\nLogin problem: {e}.")
        log("Log in again and re-run. With --browser/--cookies, log in on the site in your browser first "
            "(and export fresh cookies if you use --cookies).")
        return 2
    except Blocked as e:
        store.save()
        write_index(store)
        log(f"\nBlocked: {e}.")
        log("Open jrwishow.com in your browser, then re-run with --browser <your browser>. If it still happens, "
            "add --delay 5 to slow down.")
        return 3
    except ArchiveError as e:
        store.save()
        log(f"\nError: {e}")
        return 1

    write_index(store)
    with_problems = write_problems(store)
    posts = store.state["posts"].values()
    counts = {s: sum(1 for p in posts if p.get("status") == s) for s in ("complete", "partial", "error", "locked")}
    log("")
    log(f"Done. {counts['complete']} posts archived, {counts['partial'] + counts['error']} with errors, "
        f"{counts['locked']} above your tier.")
    if with_problems:
        log(f"Details on anything that needs attention: {store.root / 'problems.txt'}")
    log(f"Open {store.root / 'index.html'} in your browser to browse the archive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
