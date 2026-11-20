"""Shows, tabs, pairing video with audio, and the folder layout Jellyfin reads."""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Paths
from .db import Db
from .site.download import glob_escape, to_jpeg
from .site.parse import safe_filename, slugify

log = logging.getLogger("jrwi.organize")

# The site's Shows menu as of November 2026. The menu is re-read on every check; this list is
# only used until the first check, and for aliases.
DEFAULT_SHOWS = [
    ("riptide", "Riptide", []),
    ("unwitness-protection-program", "Unwitness Protection Program", ["Unwitness", "UPP"]),
    ("miller-troop-family-vacation", "Miller Troop Family Vacation", ["Miller Troop"]),
    ("judgement", "Judgement", []),
    ("total-monster-kill", "Total Monster Kill", []),
    ("the-suckening", "The Suckening", ["Suckening"]),
    ("wonderlust", "Wonderlust", []),
    ("prime-defenders", "Prime Defenders", []),
    ("blood-in-the-bayou", "Blood in the Bayou", []),
    ("apotheosis", "Apotheosis", []),
]
OTHER = ("other", "Other")

TABS = [
    ("episodes", "Episodes"),
    ("aftershow", "Just Rolled With It"),
    ("extras", "Extras"),
    ("art", "Art"),
    ("posts", "Posts"),
]
TAB_NAMES = dict(TABS)
MEDIA_KINDS = {"videos", "audio", "video_embeds"}
AUDIO_ONLY_RE = re.compile(r"[\(\[]\s*audio\s*only\s*[\)\]]|\baudio\s*only\b", re.I)
EPISODE_RE = re.compile(r"#\s*(\d{1,4})\b")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# --------------------------------------------------------------------------- shows

def ensure_default_shows(db: Db) -> None:
    if db.one("SELECT id FROM shows LIMIT 1"):
        return
    for i, (sid, name, aliases) in enumerate(DEFAULT_SHOWS):
        db.upsert("shows", "id", {"id": sid, "name": name, "sort": i, "aliases": aliases,
                                  "page_url": f"/pages/{sid}"})
    db.upsert("shows", "id", {"id": OTHER[0], "name": OTHER[1], "sort": 999, "aliases": []})


def merge_show_menu(db: Db, menu: list[dict]) -> None:
    """Add shows found in the site's menu, keeping aliases and ids of known ones."""
    if not menu:
        return
    known = {s["id"]: s for s in db.q("SELECT * FROM shows")}
    for i, entry in enumerate(menu):
        path = entry["page_url"].rstrip("/").rsplit("/", 1)[-1]
        sid = slugify(path or entry["name"])
        row = {"id": sid, "name": entry["name"], "page_url": entry["page_url"], "sort": i}
        if sid not in known:
            row["aliases"] = []
        db.upsert("shows", "id", row)
    db.upsert("shows", "id", {"id": OTHER[0], "name": OTHER[1], "sort": 999})


def show_matchers(db: Db) -> list[tuple[str, str]]:
    """(normalised alias, show id), longest first so 'Miller Troop Family Vacation' beats 'Miller Troop'."""
    pairs = []
    for s in db.q("SELECT id, name, aliases FROM shows WHERE id != 'other'"):
        for alias in [s["name"], *(s["aliases"] or [])]:
            if norm(alias):
                pairs.append((norm(alias), s["id"]))
    return sorted(pairs, key=lambda p: -len(p[0]))


def match_show(title: str, tag_names: list[str], matchers: list[tuple[str, str]]) -> str:
    t = f" {norm(title)} "
    for alias, sid in matchers:
        if f" {alias} " in t:
            return sid
    for tag in tag_names:
        tn = f" {norm(tag)} "
        for alias, sid in matchers:
            if f" {alias} " in tn:
                return sid
    return OTHER[0]


def pick_tab(post: dict, show_id: str, tag_names: list[str]) -> str:
    t = norm(post["title"])
    tags = [norm(x) for x in tag_names]
    if "just rolled with it" in t or any("rolled" in x.split() for x in tags):
        return "aftershow"
    if "art scene" in t or "art" in tags or re.search(r"\bart\b", t) and post["path_kind"] not in MEDIA_KINDS:
        return "art"
    if post["path_kind"] in MEDIA_KINDS:
        if show_id != OTHER[0] and EPISODE_RE.search(post["title"]) and not any("extra" in x for x in tags):
            return "episodes"
        return "extras"
    if post["path_kind"] == "posts" and not (post.get("body_text") or "").strip() and post.get("images"):
        return "art"
    return "posts"


def episode_number(title: str) -> int | None:
    m = EPISODE_RE.search(title or "")
    return int(m.group(1)) if m else None


def base_title(title: str) -> str:
    return norm(AUDIO_ONLY_RE.sub(" ", title or ""))


def is_audio_post(p: dict) -> bool:
    return p["path_kind"] == "audio" or bool(AUDIO_ONLY_RE.search(p["title"] or ""))


def classify_all(db: Db) -> None:
    """Assign show, tab and episode number to every post, and group video/audio versions."""
    matchers = show_matchers(db)
    tag_names = {t["id"]: t["name"] for t in db.q("SELECT id, name FROM tags")}
    posts = db.q("SELECT id, title, path_kind, tag_ids, body_text, images, published, show_override, "
                 "tab_override, episode_override, pair_id, show_id, tab, episode FROM posts")
    for p in posts:
        names = [tag_names.get(str(i), "") for i in (p["tag_ids"] or [])]
        show = p["show_override"] or match_show(p["title"], names, matchers)
        tab = p["tab_override"] or pick_tab(p, show, names)
        ep = p["episode_override"] if p["episode_override"] is not None else episode_number(p["title"])
        if (show, tab, ep) != (p["show_id"], p["tab"], p["episode"]):
            db.update("posts", "id", p["id"], {"show_id": show, "tab": tab, "episode": ep})

    # Group the versions of one episode: members-only video, YouTube embed, audio only
    groups: dict[str, list[dict]] = {}
    for p in posts:
        if p["path_kind"] in MEDIA_KINDS:
            groups.setdefault(base_title(p["title"]), []).append(p)
    rank = {"videos": 0, "video_embeds": 1, "audio": 2}
    new_pairs: dict[int, int | None] = {p["id"]: None for p in posts}
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda p: (rank.get("audio" if is_audio_post(p) else p["path_kind"], 3), p["published"] or ""))
        primary = members[0]
        for m in members[1:]:
            if _close_in_time(primary["published"], m["published"]):
                new_pairs[m["id"]] = primary["id"]
    for p in posts:
        if p["pair_id"] != new_pairs[p["id"]]:
            db.update("posts", "id", p["id"], {"pair_id": new_pairs[p["id"]]})


def _close_in_time(a: str | None, b: str | None, days: int = 21) -> bool:
    if not a or not b:
        return True
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days) <= days
    except ValueError:
        return True


# --------------------------------------------------------------------------- Jellyfin layout

def episode_title(title: str, show_name: str, aliases: list[str], tab: str = "episodes") -> str:
    """'Through the Fire | Riptide #136' -> 'Through the Fire'. Specials keep their whole title."""
    clean = AUDIO_ONLY_RE.sub(" ", title or "")
    if tab != "episodes":
        return re.sub(r"\s+", " ", clean).strip() or title
    parts = [x.strip() for x in clean.split("|")]
    names = [norm(show_name), *[norm(a) for a in aliases]]
    keep = [x for x in parts if not (EPISODE_RE.search(x) and any(n and n in norm(x) for n in names))]
    out = " | ".join(keep) if keep else clean
    out = out.strip().strip('"“”\'').strip()
    return re.sub(r"\s+", " ", out) or title


def jellyfin_target(db: Db, post: dict) -> tuple[str, int, int]:
    """(relative path without extension, season, episode) for a primary video post."""
    show = db.one("SELECT * FROM shows WHERE id=?", (post["show_id"] or "other",)) or {"name": "Other", "aliases": []}
    show_name = safe_filename(show["name"] if show["id"] != "other" else "Just Roll With It - Other", 80, keep_ext=False)
    if post["tab"] == "episodes" and post["episode"]:
        season, number = 1, int(post["episode"])
    else:
        season = 0
        number = post.get("special")
        if not number:
            row = db.one("SELECT MAX(special) AS m FROM posts WHERE show_id=?", (post["show_id"],))
            number = (row["m"] or 0) + 1 if row else 1
            db.update("posts", "id", post["id"], {"special": number})
            post["special"] = number
    title = episode_title(post["title"], show["name"], show.get("aliases") or [], post.get("tab") or "")
    stem = safe_filename(f"{show_name} - S{season:02d}E{number:02d} - {title} [jrwi-{post['id']}]", 150, keep_ext=False)
    return f"{show_name}/Season {season:02d}/{stem}", season, number


def place_video(db: Db, paths: Paths, post: dict, file: Path) -> Path:
    """Move a downloaded video to its Jellyfin location and write its .nfo and thumbnail."""
    rel, season, number = jellyfin_target(db, post)
    dest = paths.media / (rel + file.suffix.lower())
    dest.parent.mkdir(parents=True, exist_ok=True)
    if file.resolve() != dest.resolve():
        if dest.exists():
            dest.unlink()
        shutil.move(str(file), str(dest))
        old_dir = file.parent
        for sidecar in old_dir.glob(glob_escape(file.stem) + "*"):
            if sidecar.suffix in (".nfo", ".jpg", ".webp", ".png") and sidecar.name.startswith(file.stem):
                sidecar.unlink(missing_ok=True)
        _prune_empty(old_dir, paths.media)
    write_episode_nfo(db, paths, post, dest, season, number)
    write_show_nfo(db, paths, post["show_id"], dest.parent.parent)
    return dest


def _prune_empty(folder: Path, root: Path) -> None:
    try:
        folder = folder.resolve()
        root = root.resolve()
        while folder != root and root in folder.parents and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
    except OSError:
        pass


def write_episode_nfo(db: Db, paths: Paths, post: dict, video: Path, season: int, number: int) -> None:
    show = db.one("SELECT * FROM shows WHERE id=?", (post["show_id"] or "other",)) or {"name": "Other", "aliases": []}
    title = episode_title(post["title"], show["name"], show.get("aliases") or [], post.get("tab") or "")
    plot = (post.get("body_text") or "").strip()
    aired = (post.get("published") or "")[:10]
    runtime = f"<runtime>{round((post.get('duration') or 0) / 60)}</runtime>" if post.get("duration") else ""
    xml = (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<episodedetails>\n'
        f"  <title>{escape(title)}</title>\n  <showtitle>{escape(show['name'])}</showtitle>\n"
        f"  <season>{season}</season>\n  <episode>{number}</episode>\n"
        f"  <aired>{escape(aired)}</aired>\n  <premiered>{escape(aired)}</premiered>\n"
        f"  <plot>{escape(plot)}</plot>\n  {runtime}\n"
        f'  <uniqueid type="jrwi" default="true">{post["id"]}</uniqueid>\n'
        f"  <originaltitle>{escape(post['title'])}</originaltitle>\n"
        "  <lockdata>true</lockdata>\n"
        "</episodedetails>\n"
    )
    nfo = video.with_suffix(".nfo")
    if not nfo.exists() or nfo.read_text(encoding="utf-8") != xml:
        nfo.write_text(xml, encoding="utf-8")
    thumb_rel = post.get("thumbnail")
    if thumb_rel:
        src = paths.archive / thumb_rel
        dest = video.with_name(video.stem + "-thumb.jpg")
        if src.exists() and not dest.exists():
            if not to_jpeg(src, dest):
                shutil.copy(src, video.with_name(video.stem + "-thumb" + src.suffix))


def write_show_nfo(db: Db, paths: Paths, show_id: str | None, folder: Path) -> None:
    show = db.one("SELECT * FROM shows WHERE id=?", (show_id or "other",))
    if not show:
        return
    nfo = folder / "tvshow.nfo"
    xml = (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<tvshow>\n'
        f"  <title>{escape(show['name'])}</title>\n  <plot>{escape(show.get('description') or '')}</plot>\n"
        "  <studio>Just Roll With It</studio>\n"
        f'  <uniqueid type="jrwi" default="true">show-{escape(show["id"])}</uniqueid>\n'
        "  <lockdata>true</lockdata>\n"
        "</tvshow>\n"
    )
    if not nfo.exists() or nfo.read_text(encoding="utf-8") != xml:
        nfo.write_text(xml, encoding="utf-8")
    for kind in ("poster", "banner"):
        rel = show.get(kind)
        if not rel:
            continue
        src = paths.archive / rel
        dest = folder / f"{kind if kind == 'poster' else 'fanart'}.jpg"
        if src.exists() and not dest.exists():
            to_jpeg(src, dest)


def reorganize(db: Db, paths: Paths) -> int:
    """Move videos whose show, tab or number changed. Returns how many moved."""
    moved = 0
    for post in db.q("SELECT * FROM posts WHERE json_extract(media, '$.video.path') IS NOT NULL"):
        current = paths.media / post["media"]["video"]["path"]
        if not current.exists():
            continue
        rel, _, _ = jellyfin_target(db, post)
        if os.path.splitext(post["media"]["video"]["path"])[0] != rel:
            dest = place_video(db, paths, post, current)
            media = dict(post["media"])
            media["video"] = {**media["video"], "path": dest.relative_to(paths.media).as_posix()}
            db.update("posts", "id", post["id"], {"media": media})
            moved += 1
    return moved


def refresh_jellyfin(db: Db, paths: Paths) -> None:
    """Rewrite .nfo files and add thumbnails and show art that arrived after the video did."""
    folders: dict[str, Path] = {}
    for post in db.q("SELECT * FROM posts WHERE json_extract(media, '$.video.path') IS NOT NULL"):
        video = paths.media / post["media"]["video"]["path"]
        if not video.exists():
            continue
        m = re.search(r"Season (\d+)", video.parent.name)
        season = int(m.group(1)) if m else 0
        m = re.search(r"S\d+E(\d+)", video.name)
        number = int(m.group(1)) if m else 0
        write_episode_nfo(db, paths, post, video, season, number)
        folders[post["show_id"] or "other"] = video.parent.parent
    for show_id, folder in folders.items():
        write_show_nfo(db, paths, show_id, folder)
