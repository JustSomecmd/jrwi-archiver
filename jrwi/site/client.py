"""HTTP access to the members site, with polite pacing, retries and a login that survives restarts."""

from __future__ import annotations

import json
import logging
import random
import re
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

    def login(self, email: str, password: str, debug_file: Path | None = None) -> None:
        """Log in with the site's own form. On failure, explain why and save details (never the password)."""
        if not email or not password:
            raise LoginFailed("no jrwishow.com email and password are set (Admin > Settings), and the saved "
                              "login session has run out")
        notes: list[str] = []
        response_text = ""

        def fail(message: str, blocked: bool = False):
            if debug_file is not None:
                scrubbed = re.sub(r"(<input\b[^>]*?\bvalue=)(\"[^\"]*\"|'[^']*')", r'\1""', response_text)
                try:
                    debug_file.write_text("JRWI archiver login details (no password in here)\n\n" + "\n".join(notes)
                                          + "\n\n--- the site's answer (first 6000 characters) ---\n"
                                          + scrubbed[:6000], encoding="utf-8")
                except OSError:
                    pass
            raise (Blocked if blocked else LoginFailed)(message)

        self.session.cookies.clear()
        final, text = self.page(self.abs("/supporters/sign_in"))
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
        if pw is None or form is None:
            response_text = text
            fail("the login page's form is built in a way this app can't fill in. "
                 "Use 'Log in with your browser' in Settings instead.")
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
            first = form.find("input", attrs={"type": "text"}) or form.find("input", attrs={"type": None})
            if not first or not first.get("name"):
                response_text = text
                fail("couldn't find the email box on the login form")
            data = [(n, email if n == first["name"] else v) for n, v in data]
            if not any(n == first["name"] for n, _ in data):
                data.append((first["name"], email))
        submit = form.find(["button", "input"], attrs={"type": "submit"})
        if submit is not None and submit.get("name"):
            data.append((submit["name"], submit.get("value", "")))
        token_name = (meta_param.get("content") if meta_param else None) or "authenticity_token"
        headers = {"Referer": final, "Origin": self.base}
        if meta_token is not None and meta_token.get("content"):
            headers["X-CSRF-Token"] = meta_token["content"]
            if not any(n == token_name for n, _ in data):
                data.append((token_name, meta_token["content"]))
                notes.append("added the page's security token to the form")
        notes.append("fields sent: " + ", ".join(n for n, _ in data))
        action = urljoin(final, form.get("action") or final)
        r = self.request((form.get("method") or "post").upper(), action, data=data, accept=TURBO_ACCEPT,
                         headers=headers)
        response_text = r.text
        notes.append(f"sent to {action}: HTTP {r.status_code}, ended at {r.url}, {r.headers.get('Content-Type')}")
        if r.status_code == 403 and (r.headers.get("cf-mitigated") or "Just a moment" in response_text[:5000]
                                     or "challenge-platform" in response_text):
            fail("the site's bot protection stopped the login before it checked the password", blocked=True)
        if r.status_code == 429:
            fail("the site says there were too many login attempts; wait 15 to 30 minutes")
        _, feed = self.page(self.abs("/supporters"))
        if logged_in(BeautifulSoup(feed, "html.parser")):
            self.save_cookies()
            return
        after = BeautifulSoup(re.sub(r"</?template\b[^>]*>", "", response_text), "html.parser")
        message = page_message(after)
        notes.append(f"site message: {message!r}")
        if message:
            fail(f'the site said "{message}"')
        if r.status_code == 422 and not after.find("input", attrs={"type": "password"}):
            fail("the site rejected the login request itself (its security check failed), "
                 "so it never checked the email and password")
        fail(f"the site didn't log us in and gave no reason (HTTP {r.status_code})")

    def import_cookies(self, raw: str) -> int:
        """Use a login copied from a browser: a cookies.txt (Netscape) or JSON cookie export."""
        count = 0

        def keep(domain: str) -> bool:
            d = domain.lstrip(".").lower()
            return self.host == d or self.host.endswith("." + d) or d.endswith("." + self.host)

        cookies = []
        if raw.lstrip().startswith("["):
            for c in json.loads(raw):
                exp = c.get("expirationDate") or c.get("expires")
                cookies.append((c.get("domain", ""), c.get("path", "/"), bool(c.get("secure")),
                                int(exp) if exp else None, c["name"], c["value"]))
        else:
            for line in raw.splitlines():
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _sub, cpath, secure, expires, name, value = parts[:7]
                exp = int(expires) if expires.strip().isdigit() and int(expires) > 0 else None
                cookies.append((domain, cpath or "/", secure.strip().upper() == "TRUE", exp, name, value))
        found = [c for c in cookies if keep(c[0])]
        if not found:
            return 0
        self.session.cookies.clear()
        for domain, cpath, secure, exp, name, value in found:
            self.session.cookies.set_cookie(requests.cookies.create_cookie(
                name=name, value=value, domain=domain, path=cpath, secure=secure, expires=exp))
            count += 1
        self.save_cookies()
        return count


CAPTCHA_HINTS = ("cf-turnstile", "challenges.cloudflare.com/turnstile", "g-recaptcha", "recaptcha/api",
                 "hcaptcha", "data-sitekey")


def page_message(soup) -> str:
    """The site's own error or notice text, such as 'Invalid Email or password.'"""
    for sel in ("[role=alert]", "[class*=flash]", "[class*=alert]", "[class*=error]", "[class*=notice]",
                "[class*=toast]", "[class*=invalid]"):
        for el in soup.select(sel):
            t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
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
