"""Protections for every request: security headers, refusing forms sent from other sites, and slowing down
password guessing.

The archive is a private site that holds its own login, the jrwishow.com login and an API token, so:
- pages tell the browser to run only the archive's own scripts, and not to let other sites frame them;
- files from the archive that a browser could run as a page (HTML, SVG, XML) are served in a sandbox, so an
  attachment can't act as the archive;
- a change (POST, PUT, DELETE) that a browser sends from a page on another site is refused, even on the same
  machine (other services on the server share its name, so cookies alone don't tell them apart);
- after a few wrong passwords, logging in from that address waits a while.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

# The archive's own pages: its scripts only; pictures from anywhere (posts can show pictures that weren't saved);
# YouTube and Vimeo players in frames, as posts embed them.
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src * data: blob:; "
            "media-src 'self' blob: https:; font-src 'self'; connect-src 'self'; "
            "frame-src 'self' https://www.youtube.com https://youtube.com https://www.youtube-nocookie.com "
            "https://player.vimeo.com; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'self'")
# Files that a browser would run as a page, opened on their own: shown, never run
FILE_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; media-src 'self'"
ACTIVE_TYPES = ("text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
                "text/javascript", "application/javascript", "application/x-javascript", "text/xsl")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return ""


def same_origin(scope) -> bool:
    """Whether a browser sent this from one of the archive's own pages. Apps don't send Origin or Referer at all,
    and their requests are let through; a browser always sends Origin with a form or fetch that changes something."""
    origin = _header(scope, b"origin")
    if not origin:
        referer = _header(scope, b"referer")
        if not referer:
            return True
        origin = referer
    if origin == "null":
        return False
    here = (_header(scope, b"x-forwarded-host") or _header(scope, b"host")).split(",")[0].strip().lower()
    there = urlsplit(origin).netloc.lower()
    return bool(here) and there == here


class Guard:
    """ASGI middleware: headers on every response, and other sites' forms refused."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if scope.get("method") in UNSAFE_METHODS and not same_origin(scope):
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"), (b"x-content-type-options", b"nosniff")]})
            await send({"type": "http.response.body",
                        "body": b'{"error": "this change came from another site, so it was refused"}'})
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                names = {k.lower() for k, _ in headers}
                ctype = next((v.decode("latin-1").lower() for k, v in headers if k.lower() == b"content-type"), "")
                extra = [(b"x-content-type-options", b"nosniff"),
                         (b"referrer-policy", b"strict-origin-when-cross-origin"),
                         (b"x-frame-options", b"SAMEORIGIN")]
                if path.startswith("/files/"):
                    if ctype.split(";")[0].strip() in ACTIVE_TYPES:
                        extra.append((b"content-security-policy", FILE_CSP.encode()))
                elif ctype.startswith("text/html"):
                    extra.append((b"content-security-policy", PAGE_CSP.encode()))
                headers += [(k, v) for k, v in extra if k not in names]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)


class LoginThrottle:
    """Wrong passwords per address: after FREE of them within WINDOW seconds, that address waits until the oldest
    is WINDOW seconds old. Kept in memory only, for at most a few thousand addresses."""

    FREE = 5
    WINDOW = 15 * 60
    LIMIT = 5000

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}

    def _recent(self, who: str, now: float) -> list[float]:
        times = [t for t in self.failures.get(who, []) if now - t < self.WINDOW]
        if times:
            self.failures[who] = times
        else:
            self.failures.pop(who, None)
        return times

    def wait(self, who: str) -> int:
        """Seconds this address must wait before trying again, or 0."""
        now = time.monotonic()
        with self.lock:
            times = self._recent(who, now)
            if len(times) < self.FREE:
                return 0
            return max(1, int(self.WINDOW - (now - times[-self.FREE])) + 1)

    def failed(self, who: str) -> None:
        now = time.monotonic()
        with self.lock:
            if len(self.failures) >= self.LIMIT:
                for k in list(self.failures):
                    self._recent(k, now)
                if len(self.failures) >= self.LIMIT:
                    self.failures.pop(next(iter(self.failures)))
            self.failures.setdefault(who, []).append(now)

    def succeeded(self, who: str) -> None:
        with self.lock:
            self.failures.pop(who, None)


def safe_next(target: str | None) -> str:
    """Where to go after logging in: a path on this site only."""
    t = (target or "").strip()
    if (not t.startswith("/") or t.startswith("//") or "\\" in t or any(ord(c) < 32 for c in t)):
        return "/"
    return t


def safe_link(url: str | None) -> str:
    """An address from a saved page, for a link: only web addresses and the archive's own paths, never
    javascript: or data: links."""
    u = "".join(c for c in (url or "") if ord(c) > 32).strip()
    if not u:
        return "#"
    if u.startswith("/") and not u.startswith("//"):
        return u
    scheme = urlsplit(u).scheme.lower()
    return url.strip() if scheme in ("http", "https") else "#"
