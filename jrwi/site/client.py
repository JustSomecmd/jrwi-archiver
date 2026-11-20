"""HTTP access to the members site, with polite pacing, retries and a login that survives restarts."""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("jrwi.client")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
TURBO_ACCEPT = "text/vnd.turbo-stream.html, text/html, application/xhtml+xml"


class SiteError(Exception):
    pass


class SessionExpired(SiteError):
    pass


class LoginFailed(SiteError):
    pass


class Blocked(SiteError):
    pass


class NotFound(SiteError):
    pass


class Client:
    def __init__(self, base: str, cookie_file: Path, delay: float = 1.5) -> None:
        self.base = base.rstrip("/")
        self.host = (urlparse(self.base).hostname or "").lower()
        self.cookie_file = cookie_file
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        self._last = 0.0
        self.should_stop: Callable[[], bool] = lambda: False
        self.load_cookies()

    # ------------------------------------------------------------------ helpers

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

    # ------------------------------------------------------------------ cookies

    def load_cookies(self) -> None:
        if not self.cookie_file.exists():
            return
        try:
            for c in json.loads(self.cookie_file.read_text()):
                self.session.cookies.set_cookie(requests.cookies.create_cookie(
                    name=c["name"], value=c["value"], domain=c["domain"], path=c.get("path", "/"),
                    secure=c.get("secure", False), expires=c.get("expires")))
        except (ValueError, KeyError) as e:
            log.warning("ignoring unreadable cookie file: %s", e)

    def save_cookies(self) -> None:
        items = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                  "secure": c.secure, "expires": c.expires} for c in self.session.cookies]
        tmp = self.cookie_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(items))
        tmp.replace(self.cookie_file)
        try:
            self.cookie_file.chmod(0o600)
        except OSError:
            pass

    def clear_cookies(self) -> None:
        self.session.cookies.clear()
        self.cookie_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------ requests

    def request(self, method: str, url: str, *, accept: str | None = None, headers: dict | None = None,
                data=None, stream: bool = False, pace: bool = True, tries: int = 6,
                timeout: tuple = (20, 120)) -> requests.Response:
        h = dict(headers or {})
        if accept:
            h["Accept"] = accept
        problem = ""
        for attempt in range(tries):
            if self.should_stop():
                raise SiteError("stopped")
            if pace:
                self._pace()
            try:
                r = self.session.request(method, url, headers=h, data=data, stream=stream, timeout=timeout)
            except requests.RequestException as e:
                problem = type(e).__name__
            else:
                self._last = time.monotonic()
                if r.status_code != 429 and r.status_code < 500:
                    return r
                problem = f"HTTP {r.status_code}"
                retry_after = r.headers.get("Retry-After", "")
                r.close()
                if retry_after.isdigit():
                    time.sleep(min(int(retry_after), 600))
                    continue
            self._last = time.monotonic()
            if attempt < tries - 1:
                wait = min(300, 5 * 2 ** attempt)
                log.info("connection problem with %s (%s), retrying in %ss", urlparse(url).hostname, problem, wait)
                time.sleep(wait)
        raise SiteError(f"gave up after {tries} tries ({problem}): {url}")

    def page(self, url: str, accept: str = HTML_ACCEPT, headers: dict | None = None) -> tuple[str, str]:
        """GET a page on the members site. Returns (final URL, text)."""
        r = self.request("GET", url, accept=accept, headers=headers)
        text = r.text
        if r.status_code == 403 and (r.headers.get("cf-mitigated") or "Just a moment" in text[:5000]
                                     or "challenge-platform" in text):
            raise Blocked("the site answered with a bot check instead of the page")
        if r.status_code == 404:
            raise NotFound(f"HTTP 404 for {url}")
        if r.status_code >= 400:
            raise SiteError(f"HTTP {r.status_code} for {url}")
        final = r.url
        if (urlparse(final).path.rstrip("/").endswith("/sign_in")
                and not urlparse(url).path.rstrip("/").endswith("/sign_in")):
            raise SessionExpired("the site sent us to the login page")
        return final, text

    # ------------------------------------------------------------------ login

    def login(self, email: str, password: str) -> None:
        if not email or not password:
            raise LoginFailed("no jrwishow.com email and password are set (Admin > Settings)")
        self.session.cookies.clear()
        final, text = self.page(self.abs("/supporters/sign_in"))
        soup = BeautifulSoup(text, "html.parser")
        pw = soup.find("input", attrs={"type": "password"})
        form = pw.find_parent("form") if pw else None
        if not form:
            raise LoginFailed("couldn't find the login form on /supporters/sign_in")
        data: list[tuple[str, str]] = []
        email_done = False
        for inp in form.find_all(["input", "select", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            typ = (inp.get("type") or "text").lower()
            if typ == "password":
                data.append((name, password))
            elif typ == "email" or ("email" in name.lower() and typ in ("text", "email")):
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
            first = form.find("input", attrs={"type": "text"})
            if not first or not first.get("name"):
                raise LoginFailed("couldn't find the email field on the login form")
            data = [(n, email if n == first["name"] else v) for n, v in data]
        submit = form.find(["button", "input"], attrs={"type": "submit"})
        if submit is not None and submit.get("name"):
            data.append((submit["name"], submit.get("value", "")))
        action = urljoin(final, form.get("action") or final)
        r = self.request((form.get("method") or "post").upper(), action, data=data, accept=HTML_ACCEPT,
                         headers={"Referer": final, "Origin": self.base})
        if r.status_code == 403 and ("Just a moment" in r.text[:5000] or r.headers.get("cf-mitigated")):
            raise Blocked("the login page answered with a bot check")
        after = BeautifulSoup(r.text, "html.parser")
        if r.status_code >= 400 or (after.find("input", attrs={"type": "password"})
                                    and urlparse(r.url).path.rstrip("/").endswith("/sign_in")):
            raise LoginFailed("the site didn't accept the email and password")
        self.save_cookies()


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
