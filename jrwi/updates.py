"""Whether a newer version of the archiver has been published (since 0.14.12).

Once a day the archiver asks GitHub's container registry which versions of its image there are: only images that
have been built and can be pulled count. It asks GitHub, too, for the titles of the release commits since this
version, as "what's new". Nothing about the archive is sent, only the requests themselves. A new version shows on the
admin page and in the header, in the API's status (contract v0.30), and once, as a message to the notification
address when one is set."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from . import VERSION

log = logging.getLogger("jrwi.updates")

IMAGE = "justsomecmd/jrwi-archiver"
REPO = "JustSomecmd/jrwi-archiver"
REGISTRY = "https://ghcr.io"
GITHUB_API = "https://api.github.com"
TIMEOUT = (10, 30)
EVERY_HOURS = 24
# Run in the folder with the compose file that has the archiver in it
COMMAND = "docker compose pull jrwi-archiver && docker compose up -d jrwi-archiver"
HEADERS = {"User-Agent": f"jrwi-archiver/{VERSION}"}


def parse(version: str | None) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (version or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a: str | None, b: str | None) -> bool:
    """Whether version a comes after version b."""
    pa, pb = parse(a), parse(b)
    return bool(pa and pb and pa > pb)


def published(get=requests.get) -> list[str]:
    """Every version of the image on the registry (its tags of the form 1.2.3), oldest first."""
    r = get(f"{REGISTRY}/token", params={"scope": f"repository:{IMAGE}:pull", "service": "ghcr.io"},
            headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    token = r.json().get("token") or r.json().get("access_token")
    headers = dict(HEADERS, Authorization=f"Bearer {token}")
    url, tags = f"{REGISTRY}/v2/{IMAGE}/tags/list?n=1000", []
    for _ in range(20):   # the list comes in pages, linked from each answer's Link header
        r = get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        tags += r.json().get("tags") or []
        nxt = (getattr(r, "links", None) or {}).get("next", {}).get("url")
        if not nxt:
            break
        url = nxt if nxt.startswith("http") else REGISTRY + nxt
    return sorted({t for t in tags if parse(t)}, key=parse)


def notes(after: str, upto: str, get=requests.get) -> list[dict]:
    """What's new: the titles of the release commits ("0.14.11: lines that begin after a pause ...") for the versions
    after `after`, up to and including `upto`, oldest first."""
    r = get(f"{GITHUB_API}/repos/{REPO}/commits", params={"per_page": 100},
            headers=dict(HEADERS, Accept="application/vnd.github+json"), timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for c in r.json():
        title = ((c.get("commit") or {}).get("message") or "").strip().splitlines()[:1]
        m = re.match(r"(\d+\.\d+\.\d+)(?: and \d+\.\d+\.\d+)?: (.+)", title[0] if title else "")
        if m and newer(m.group(1), after) and not newer(m.group(1), upto):
            out.append({"version": m.group(1), "text": m.group(2)[:300]})
    out.reverse()   # GitHub lists the newest first
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check(db, get=requests.get, post=requests.post) -> dict:
    """Ask which versions there are, keep the answer, and send the notification for a new one, once."""
    saved = db.setting("update_state") or {}
    state = {"latest": saved.get("latest"), "notes": saved.get("notes") or [], "notified": saved.get("notified"),
             "checked": _now(), "error": None}
    try:
        versions = published(get)
        if versions:
            state["latest"] = versions[-1]
    except (requests.RequestException, ValueError) as e:
        state["error"] = f"Couldn't ask GitHub's container registry: {_short(e)}"
        log.warning("checking for a new version failed: %s", e)
    if newer(state["latest"], VERSION) and (state["latest"] != saved.get("latest") or not state["notes"]):
        try:
            state["notes"] = notes(VERSION, state["latest"], get)
        except (requests.RequestException, ValueError) as e:
            log.info("what's new in %s couldn't be read: %s", state["latest"], e)
            state["notes"] = []
    db.set_setting("update_state", state)
    if newer(state["latest"], VERSION) and state.get("notified") != state["latest"]:
        if _notify(db, state, post):
            state["notified"] = state["latest"]
            db.set_setting("update_state", state)
    return status(db)


def _short(e: Exception) -> str:
    text = str(e)
    return text if len(text) < 160 else text[:157] + "..."


def _notify(db, state: dict, post) -> bool:
    """One message to the notification address (ntfy or Discord) per new version. True when sent, or when there's
    no address to send to (so a later address doesn't get a message about an old version)."""
    url = (db.setting("notify_url") or "").strip()
    if not url:
        return True
    lines = [f"JRWI archiver {state['latest']} is out. This server runs {VERSION}."]
    for n in [n for n in state.get("notes") or [] if newer(n["version"], VERSION)][-8:]:
        lines.append(f"- {n['version']}: {n['text']}")
    lines.append(f"To update, in the folder with the compose file: {COMMAND}")
    from .notify import send
    return send(url, "\n".join(lines), title="JRWI archiver update", post=post)


def status(db) -> dict | None:
    """What the admin page, the header and the API show: None while checking is off in Settings."""
    if not db.setting("update_check"):
        return None
    s = db.setting("update_state") or {}
    latest = s.get("latest")
    return {"version": VERSION, "latest": latest, "available": newer(latest, VERSION),
            "checked": s.get("checked"), "error": s.get("error"),
            "notes": [n for n in s.get("notes") or [] if newer(n.get("version"), VERSION)
                      and not newer(n.get("version"), latest)],
            "command": COMMAND}
