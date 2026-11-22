"""Turning database rows into API objects (contract v0.1), and making stored HTML safe to show."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from ..config import Paths
from ..db import Db
from ..organize import TAB_NAMES, TABS, is_audio_post
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
                        "url": f"/api/video/{m['id']}", "height": v.get("height"), "size": v.get("size"),
                        "duration": v.get("duration")}
        for m in members:
            for e in m["embeds"] or []:
                if e.get("kind") == "youtube" and e.get("youtube_id"):
                    out = {"post_id": m["id"], "youtube_id": e["youtube_id"], "youtube_url": e["url"],
                           "duration": m["duration"]}
                    # The item's archived video was deleted because TubeArchivist has this one (contract v0.15)
                    done = next((x for x in members if (x.get("handover") or {}).get("status") == "done"), None)
                    if done:
                        out["handed_over"] = {"at": done["handover"].get("at"), "post_id": done["id"]}
                    return out
        return None

    def _audio(self, members: list[dict]) -> dict | None:
        for m in members:
            a = (m["media"] or {}).get("audio")
            if a and (self.paths.archive / a["path"]).exists():
                return {"post_id": m["id"], "url": f"/api/audio/{m['id']}", "mime": a.get("mime"),
                        "size": a.get("size"), "duration": a.get("duration") or m["duration"]}
        return None

    @staticmethod
    def sync_of(primary: dict, video: dict | None, audio: dict | None) -> dict | None:
        """How far apart an item's video and audio are (audio position = video position + offset), or None when it
        has only one of them. Unchecked pairs, like YouTube-only videos, get an offset of 0."""
        if not video or not audio:
            return None
        s = primary.get("sync") or {}
        if s.get("video_post") == video["post_id"] and s.get("audio_post") == audio["post_id"] and s.get("checked"):
            return {"offset": s["offset"], "checked": True}
        return {"offset": 0.0, "checked": False}

    def summary(self, primary: dict, members: list[dict] | None = None) -> dict:
        members = members or self.members(primary)
        video, audio = self._video(members), self._audio(members)
        tab = primary["tab"] or "posts"
        tabs = primary.get("tabs") or [tab]
        if tab == "episodes" and (video or audio):
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
        durations = [m["duration"] for m in members if m["duration"]] + [
            p["duration"] for p in (video, audio) if p and p.get("duration")]
        return {
            "id": primary["id"], "title": primary["title"], "date": iso(primary["published"]),
            "show": primary["show_id"] or "other", "tab": tab, "tabs": tabs,
            "places": [{"show": p.split(":", 1)[0], "tab": p.split(":", 1)[1]} for p in (primary.get("places") or [])],
            "kind": kind, "episode": primary["episode"],
            "season": ((primary.get("season") or 1) if "episodes" in tabs and primary["episode"]
                       else primary.get("season") if "aftershow" in tabs else None),
            "pinned": bool(primary["pinned"]), "thumbnail": files_url(thumb),
            "duration": max(durations) if durations else None, "video": video, "audio": audio,
            "sync": self.sync_of(primary, video, audio),
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

    @staticmethod
    def version_of(post: dict) -> str | None:
        """Which version of an episode a post is: 'video' (members-only video), 'audio' or 'youtube'.
        None for posts without a video or audio."""
        if is_audio_post(post):
            return "audio"
        if post["path_kind"] == "videos":
            return "video"
        if post["path_kind"] == "video_embeds":
            return "youtube"
        media = post["media"] or {}
        if media.get("video"):
            return "video"
        if media.get("audio"):
            return "audio"
        if any(e.get("kind") == "youtube" for e in post["embeds"] or []):
            return "youtube"
        return None

    def versions(self, post: dict) -> list[dict]:
        """Every post of the episode this post belongs to, main post first, each with its own counts."""
        primary = self.primary_of(post["id"]) or post
        return [{"post_id": m["id"], "version": self.version_of(m), "title": m["title"], "url": m["url"],
                 "date": iso(m["published"]), "like_count": m["like_count"], "comment_count": m["comment_count"],
                 "main": m["id"] == primary["id"], "removed_from_site": m.get("site_gone")}
                for m in self.members(primary)]

    @staticmethod
    def handed_over(post: dict) -> dict | None:
        """For a post whose archived video was deleted because TubeArchivist has it from YouTube (contract v0.15)."""
        h = post.get("handover") or {}
        if h.get("status") != "done":
            return None
        return {"at": h.get("at"), "youtube_id": h.get("youtube_id"), "youtube_url": h.get("youtube_url"),
                "post_id": h.get("youtube_post")}

    def post_flags(self, post: dict) -> dict:
        item_id = post["pair_id"] or post["id"]
        others = self.db.one("SELECT COUNT(*) AS n FROM posts WHERE (pair_id=? OR id=?) AND id != ?",
                             (item_id, item_id, post["id"]))["n"]
        return {"version": self.version_of(post), "item_id": item_id, "paired": bool(others),
                "removed_from_site": post.get("site_gone"), "handed_over": self.handed_over(post)}

    def post_summary(self, post: dict) -> dict:
        """One post on its own: its own likes, comment count and files, never merged with its other versions."""
        out = self.summary(post, [post])
        out.update(self.post_flags(post))
        return out

    def post_full(self, post: dict) -> dict:
        out = self.full(post, [post])
        out.update(self.post_flags(post))
        return out

    def full(self, primary: dict, members: list[dict] | None = None) -> dict:
        members = members or self.members(primary)
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
            "versions": self.versions(primary),
            "problems": [p for m in members for p in (m["problems"] or [])],
        })
        return out

    @staticmethod
    def author(r: dict) -> dict:
        """A comment's author as the site shows them: name, picture, badges and the colour of their tier."""
        def local(path):
            return files_url(path) if path and not path.startswith("http") else path

        badges = [{"name": b.get("name"), "icon": b.get("icon"), "image": local(b.get("image"))}
                  for b in (r.get("author_badges") or [])]
        return {"id": r.get("author_id"), "name": r["author_name"] or "", "avatar": local(r["author_avatar"]),
                "badges": badges, "color": r.get("author_color")}

    def comments(self, primary: dict, page: int = 1, page_size: int = 50, only_post: int | None = None) -> dict:
        """Threaded comments. For an item: from every version, the main post's first. With only_post: just the
        comments on that one post."""
        ids = [only_post] if only_post else [m["id"] for m in self.members(primary)]
        rows = self.db.q(f"SELECT * FROM comments WHERE post_id IN ({','.join('?' * len(ids))}) "
                         "ORDER BY post_id = ? DESC, position", ids + [primary["id"]])
        by_parent: dict[str | None, list[dict]] = {}
        for r in rows:
            by_parent.setdefault(r["parent_id"], []).append(r)

        def build(r: dict) -> dict:
            return {"id": r["id"], "post_id": r["post_id"], "author": self.author(r),
                    "date": iso(r["date"]), "date_text": r["date_text"],
                    "body_html": clean_html(r["body_html"]), "body_text": r["body_text"] or "",
                    "like_count": r["like_count"], "pinned": bool(r.get("pinned")), "link": r.get("link"),
                    "replies": [build(c) for c in by_parent.get(r["id"], [])]}

        known_ids = {r["id"] for r in rows}
        top = [r for r in rows if r["parent_id"] is None or r["parent_id"] not in known_ids]
        top.sort(key=lambda r: not r.get("pinned"))  # pinned comments first, like the site
        start = (page - 1) * page_size
        return {"items": [build(r) for r in top[start:start + page_size]], "page": page, "page_size": page_size,
                "total": len(top)}

    def show(self, row: dict, with_tabs: bool = False, separate: bool = False) -> dict:
        """A show. Counts are of items (versions grouped), or of single posts with separate=True."""
        grouped = "" if separate else "AND pair_id IS NULL "
        stats = self.db.one("SELECT COUNT(*) AS n, MAX(published) AS latest FROM posts WHERE (show_id=? OR places LIKE ?) "
                            f"{grouped}AND status NOT IN ('locked', 'gone')", (row["id"], f'%"{row["id"]}:%'))
        art = files_url(row.get("art"))
        out = {"id": row["id"], "name": row["name"], "description": row["description"] or "",
               # v0.12: the show's picture (poster and banner are the same one), and image never empty
               "poster": art, "banner": art, "image": art or files_url(self.db.setting("site_logo")),
               "item_count": stats["n"], "latest": iso(stats["latest"])}
        if with_tabs:
            counts: dict[str, int] = {}
            per_season: dict[str, dict[int, int]] = {"episodes": {}, "aftershow": {}}
            sid = row["id"]
            for r in self.db.q(f"SELECT show_id, tab, tabs, places, season FROM posts WHERE (show_id=? OR places LIKE ?) "
                               f"{grouped}AND status NOT IN ('locked', 'gone')", (sid, f'%"{sid}:%')):
                here = {p.split(":", 1)[1] for p in (r["places"] or []) if p.split(":", 1)[0] == sid}
                if r["show_id"] == sid:
                    here |= set(r["tabs"] or [r["tab"]])
                for t in here:
                    counts[t] = counts.get(t, 0) + 1
                for t in ("episodes", "aftershow"):
                    if t in here and r["show_id"] == sid and (r["season"] or t == "episodes"):
                        per_season[t][r["season"] or 1] = per_season[t].get(r["season"] or 1, 0) + 1
            # v0.13: the seasons of the show's Episodes, with the name all their episodes share when they share one;
            # v0.14: and of its Just Rolled With It
            names = {s["season"]: s.get("name") for s in (row.get("seasons") or [])}
            seasons_of = {t: [{"season": n, "name": names.get(n), "count": c} for n, c in sorted(per_season[t].items())]
                          for t in per_season}
            out["tabs"] = [{"id": t, "name": name, "count": counts[t], **({"seasons": seasons_of[t]} if t in seasons_of else {})}
                           for t, name in TABS if counts.get(t)]
            out["seasons"] = seasons_of["episodes"]
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
