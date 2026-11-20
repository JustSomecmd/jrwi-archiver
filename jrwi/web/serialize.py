"""Turning database rows into API objects (contract v0.1), and making stored HTML safe to show."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from ..config import Paths
from ..db import Db
from ..organize import TAB_NAMES, TABS
from ..site.parse import url_key

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "figure", "figcaption", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "s", "small", "span", "strong", "sub", "sup", "table",
    "tbody", "td", "th", "thead", "tr", "u", "ul", "section", "article", "header", "footer", "main", "nav",
    "picture", "source", "video", "audio", "iframe",
}
ALLOWED_ATTRS = {"href", "src", "srcset", "alt", "title", "width", "height", "class", "colspan", "rowspan",
                 "target", "rel", "controls", "poster", "type", "loading"}
SAFE_IFRAME_HOSTS = ("www.youtube.com", "youtube.com", "www.youtube-nocookie.com", "player.vimeo.com")


def files_url(rel: str | None) -> str | None:
    return "/files/" + quote(rel) if rel else None


def iso(d: str | None) -> str | None:
    if not d:
        return None
    return d if "T" in d else f"{d}T00:00:00+00:00"


def clean_html(html: str | None, local_images: dict[str, str] | None = None) -> str:
    """Keep the formatting, drop scripts and event handlers, and point images at local copies."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "form", "input", "button", "noscript", "template", "svg"]):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        if tag.name == "iframe" and (urlparse(tag.get("src", "")).hostname or "") not in SAFE_IFRAME_HOSTS:
            tag.decompose()
            continue
        for attr in list(tag.attrs):
            val = tag.attrs[attr]
            sval = " ".join(val) if isinstance(val, list) else str(val)
            if attr not in ALLOWED_ATTRS or sval.strip().lower().startswith(("javascript:", "data:text")):
                del tag.attrs[attr]
        if local_images:
            for attr in ("src", "href"):
                if tag.get(attr) and url_key(tag[attr]) in local_images:
                    tag[attr] = local_images[url_key(tag[attr])]
        if tag.name == "a" and tag.get("href", "").startswith("http"):
            tag["target"] = "_blank"
            tag["rel"] = "noopener noreferrer"
    return str(soup)


class Serializer:
    def __init__(self, db: Db, paths: Paths) -> None:
        self.db = db
        self.paths = paths

    def members(self, primary: dict) -> list[dict]:
        return [primary] + self.db.q("SELECT * FROM posts WHERE pair_id=? ORDER BY id", (primary["id"],))

    def primary_of(self, post_id: int) -> dict | None:
        p = self.db.post(post_id)
        if p and p["pair_id"]:
            return self.db.post(p["pair_id"]) or p
        return p

    def _video(self, members: list[dict]) -> dict | None:
        for m in members:
            v = (m["media"] or {}).get("video")
            if v and (self.paths.media / v["path"]).exists():
                return {"post_id": m["id"], "jellyfin_path": str(self.paths.media / v["path"]),
                        "url": f"/api/video/{m['id']}", "height": v.get("height"), "size": v.get("size")}
        for m in members:
            for e in m["embeds"] or []:
                if e.get("kind") == "youtube" and e.get("youtube_id"):
                    return {"post_id": m["id"], "youtube_id": e["youtube_id"], "youtube_url": e["url"]}
        return None

    def _audio(self, members: list[dict]) -> dict | None:
        for m in members:
            a = (m["media"] or {}).get("audio")
            if a and (self.paths.archive / a["path"]).exists():
                return {"post_id": m["id"], "url": f"/api/audio/{m['id']}", "mime": a.get("mime"),
                        "size": a.get("size")}
        return None

    def summary(self, primary: dict, members: list[dict] | None = None) -> dict:
        members = members or self.members(primary)
        video, audio = self._video(members), self._audio(members)
        tab = primary["tab"] or "posts"
        if tab == "episodes":
            kind = "episode"
        elif video and video.get("jellyfin_path"):
            kind = "video"
        elif audio:
            kind = "audio"
        elif video:
            kind = "youtube"
        elif tab == "art":
            kind = "art"
        else:
            kind = "post"
        thumb = next((m["thumbnail"] for m in members if m["thumbnail"]), None)
        if not thumb:
            first_img = next((i["url"] for m in members for i in (m["images"] or [])), None)
            thumb = first_img
        durations = [m["duration"] for m in members if m["duration"]]
        return {
            "id": primary["id"], "title": primary["title"], "date": iso(primary["published"]),
            "show": primary["show_id"] or "other", "tab": tab, "kind": kind, "episode": primary["episode"],
            "pinned": bool(primary["pinned"]), "thumbnail": files_url(thumb),
            "duration": max(durations) if durations else None, "video": video, "audio": audio,
            "comment_count": primary["comment_count"], "like_count": primary["like_count"],
            "locked": bool(primary["locked"]) or primary["status"] == "locked",
            "archived": primary["status"] in ("complete", "partial"),
        }

    def local_images(self, members: list[dict]) -> dict[str, str]:
        out = {}
        for m in members:
            for img in m["images"] or []:
                if img.get("source"):
                    out[url_key(img["source"])] = files_url(img["url"])
        return out

    def full(self, primary: dict) -> dict:
        members = self.members(primary)
        out = self.summary(primary, members)
        tag_names = {t["id"]: t["name"] for t in self.db.q("SELECT id, name FROM tags")}
        local = self.local_images(members)
        seen: set[str] = set()

        def collect(field: str, key: str = "url") -> list[dict]:
            res = []
            for m in members:
                for x in m[field] or []:
                    if x.get(key) not in seen:
                        seen.add(x.get(key))
                        res.append(x)
            return res

        out.update({
            "body_html": clean_html(primary["body_html"], local),
            "body_text": primary["body_text"] or "",
            "original_url": primary["url"],
            "tags": [tag_names.get(str(t), str(t)) for t in (primary["tag_ids"] or [])],
            "access": primary["access"],
            "images": [{"url": files_url(i["url"])} for i in collect("images")],
            "attachments": [{"name": a["name"], "url": files_url(a["url"]), "size": a.get("size")}
                            for a in collect("attachments")],
            "links": collect("links"),
            "documents": [{"name": d["name"], "url": files_url(d["url"])} for d in collect("documents")],
            "embeds": [{"kind": e["kind"], "url": e["url"]} for e in collect("embeds")],
            "versions": [{"post_id": m["id"], "title": m["title"], "url": m["url"]} for m in members],
            "problems": [p for m in members for p in (m["problems"] or [])],
        })
        return out

    def comments(self, primary: dict, page: int = 1, page_size: int = 50) -> dict:
        ids = [m["id"] for m in self.members(primary)]
        rows = self.db.q(f"SELECT * FROM comments WHERE post_id IN ({','.join('?' * len(ids))}) "
                         "ORDER BY post_id = ? DESC, position", ids + [primary["id"]])
        by_parent: dict[str | None, list[dict]] = {}
        for r in rows:
            by_parent.setdefault(r["parent_id"], []).append(r)

        def build(r: dict) -> dict:
            avatar = r["author_avatar"]
            return {"id": r["id"], "author": {"name": r["author_name"] or "",
                                              "avatar": files_url(avatar) if avatar and not avatar.startswith("http") else avatar},
                    "date": iso(r["date"]), "date_text": r["date_text"],
                    "body_html": clean_html(r["body_html"]), "body_text": r["body_text"] or "",
                    "like_count": r["like_count"], "replies": [build(c) for c in by_parent.get(r["id"], [])]}

        known_ids = {r["id"] for r in rows}
        top = [r for r in rows if r["parent_id"] is None or r["parent_id"] not in known_ids]
        start = (page - 1) * page_size
        return {"items": [build(r) for r in top[start:start + page_size]], "page": page, "page_size": page_size,
                "total": len(top)}

    def show(self, row: dict, with_tabs: bool = False) -> dict:
        stats = self.db.one("SELECT COUNT(*) AS n, MAX(published) AS latest FROM posts WHERE show_id=? "
                            "AND pair_id IS NULL AND status NOT IN ('locked', 'gone')", (row["id"],))
        out = {"id": row["id"], "name": row["name"], "description": row["description"] or "",
               "poster": files_url(row["poster"]), "banner": files_url(row["banner"]),
               "item_count": stats["n"], "latest": iso(stats["latest"])}
        if with_tabs:
            counts = {r["tab"]: r["n"] for r in self.db.q(
                "SELECT tab, COUNT(*) AS n FROM posts WHERE show_id=? AND pair_id IS NULL "
                "AND status NOT IN ('locked', 'gone') GROUP BY tab", (row["id"],))}
            out["tabs"] = [{"id": t, "name": name, "count": counts[t]} for t, name in TABS if counts.get(t)]
        return out


def tab_name(tab: str) -> str:
    return TAB_NAMES.get(tab, tab.title())


def safe_join(root: Path, rel: str) -> Path | None:
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p
