"""Shows, tabs, pairing video with audio, and the folder layout Jellyfin reads."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Paths
from .db import Db
from .site.download import glob_escape, to_jpeg, to_poster
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
# Which tab a show's tag puts a post in, as on the site: the plain tag ("RIPTIDE") is the episodes, "<show> Extras"
# the extras, "<show> Rolled" the aftershow. A post with several of a show's tags is in each of those tabs.
TAG_SUFFIX_TABS = {"": "episodes", "early access": "episodes", "extras": "extras", "extra": "extras",
                   "rolled": "aftershow"}
SEASON_TAG_RE = re.compile(r"season \d{1,2}")
# Tags that aren't about one show but still say what a post is
GENERIC_TAG_TABS = {"just rolled with it": "aftershow", "art": "art", "thumbnails": "art"}
# Which of a post's tabs is its main one (for Jellyfin and the API's tab): videos and audio by what they are,
# other posts by what they hold
MAIN_TAB_ORDER = {True: ["aftershow", "extras", "episodes", "art", "posts"],
                  False: ["art", "extras", "aftershow", "posts", "episodes"]}
MEDIA_KINDS = {"videos", "audio", "video_embeds"}
# "(AUDIO ONLY)", "(Audio Only)", "(Audio)", "[Audio]"
AUDIO_ONLY_RE = re.compile(r"[\(\[]\s*audio(?:\s*only)?\s*[\)\]]|\baudio\s*only\b", re.I)
# Labels that say which version a post is, left out when matching versions: the above plus "(Video)"
VERSION_LABEL_RE = re.compile(r"[\(\[]\s*(?:audio(?:\s*only)?|video)\s*[\)\]]|\baudio\s*only\b", re.I)
# How titles number episodes: "Riptide #136", "Season 2 Episode 1", "S2E1", "S2 #3", "Episode 12", "Ep. 12"
SEASON_EPISODE_RES = [
    re.compile(r"\bS(?:eason)?\s*(\d{1,2})\s*[,:|-]?\s*E(?:p(?:isode)?)?\.?\s*(\d{1,4})\b", re.I),
    re.compile(r"\bS(?:eason)?\s*(\d{1,2})\s*[,:|-]?\s*#\s*(\d{1,4})\b", re.I),
]
EPISODE_RES = [re.compile(r"#\s*(\d{1,4})\b"), re.compile(r"\b(?:Episode|Ep)\.?\s*(\d{1,4})\b", re.I)]
SEASON_RE = re.compile(r"\bSeason\s*(\d{1,2})\b|\bS(\d{1,2})\b", re.I)


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
        row = {"id": sid, "name": entry["name"], "page_url": entry["page_url"], "sort": i, "source": "menu"}
        if sid not in known:
            row["aliases"] = []
        db.upsert("shows", "id", row)
    db.upsert("shows", "id", {"id": OTHER[0], "name": OTHER[1], "sort": 999})


def shared_show_art(db: Db) -> set[str]:
    """File names of pictures that several shows' pages carry, such as the site-wide share picture. They aren't
    any show's own art. Remembered, so a page saved again later can't bring one back."""
    known = set(db.setting("shared_show_art") or [])
    seen: dict[str, set[str]] = {}
    for s in db.q("SELECT id, poster, banner FROM shows"):
        for rel in (s["poster"], s["banner"]):
            if rel:
                seen.setdefault(Path(rel).name, set()).add(s["id"])
    shared = known | {name for name, ids in seen.items() if len(ids) > 1}
    if shared != known:
        db.set_setting("shared_show_art", sorted(shared))
    return shared


def drop_shared_show_art(db: Db) -> int:
    """Forget show pictures that are really the site's own picture. Returns how many were dropped. A picture the
    user picked stays."""
    shared = shared_show_art(db)
    dropped = 0
    for s in db.q("SELECT id, poster, banner, art, art_source FROM shows"):
        fields = {k: None for k in ("poster", "banner") if s[k] and Path(s[k]).name in shared}
        if s["art"] and s["art_source"] != "picked" and Path(s["art"]).name in shared:
            fields.update(art=None, art_source=None)
        if fields:
            db.update("shows", "id", s["id"], fields)
            dropped += len(fields)
    return dropped


ART_SOURCES = {"page": "the show's own picture on its page on the site", "picked": "picked by you",
               "kept": "kept from before 0.6.0, when pictures further down a show's page were used too"}


def show_art(db: Db, show: dict) -> str | None:
    """A show's picture: its page's own share picture, or the one picked on the admin page. None when it has none,
    and the JRWI logo is shown instead."""
    return show.get("art") or None


def site_logo(db: Db) -> str | None:
    return db.setting("site_logo") or None


def choose_show_art(db: Db, show_id: str, choice: str) -> None:
    """The admin page's choice: "auto" (the page's own share picture, when it has one), "logo", or one of the
    pictures on the show's page. A picked picture (or the logo) stays until another is picked."""
    show = db.one("SELECT * FROM shows WHERE id=?", (show_id,))
    if not show:
        raise ValueError("no such show")
    if choice == "auto":
        shared = shared_show_art(db)
        own = show["poster"] if show["poster"] and Path(show["poster"]).name not in shared else None
        db.update("shows", "id", show_id, {"art": own, "art_source": "page" if own else None})
    elif choice == "logo":
        db.update("shows", "id", show_id, {"art": None, "art_source": "picked"})
    elif choice in art_choices(show):
        db.update("shows", "id", show_id, {"art": choice, "art_source": "picked"})
    else:
        raise ValueError("that picture isn't one of the show's")


def art_choices(show: dict) -> list[str]:
    """Pictures found on a show's page, plus its current one."""
    out = []
    for rel in [show.get("art"), *(show.get("art_options") or [])]:
        if rel and rel not in out:
            out.append(rel)
    return out


def newest_thumbnail(db: Db, show_id: str) -> str | None:
    """The newest archived thumbnail of a show: its picture until the site gives it art of its own."""
    row = db.one("SELECT thumbnail FROM posts WHERE show_id=? AND thumbnail IS NOT NULL "
                 "AND status IN ('complete', 'partial') ORDER BY published DESC, id DESC LIMIT 1", (show_id,))
    return row["thumbnail"] if row else None


def show_matchers(db: Db) -> list[tuple[str, str]]:
    """(normalised alias, show id), longest first so 'Miller Troop Family Vacation' beats 'Miller Troop'."""
    pairs = []
    for s in db.q("SELECT id, name, aliases FROM shows WHERE id != 'other'"):
        for alias in [s["name"], *(s["aliases"] or [])]:
            if norm(alias):
                pairs.append((norm(alias), s["id"]))
    return sorted(pairs, key=lambda p: -len(p[0]))


def acronym(name: str) -> str | None:
    """"Total Monster Kill" -> "tmk": the site tags some shows by their initials ("TMK Extras")."""
    words = norm(name).split()
    return "".join(w[0] for w in words) if len(words) >= 3 else None


def show_tag_names(db: Db) -> list[tuple[str, str]]:
    """(normalised name a show's tags start with, show id), longest first."""
    pairs = []
    for s in db.q("SELECT id, name, aliases FROM shows WHERE id != 'other'"):
        for n in {norm(x) for x in [s["name"], *(s["aliases"] or []), acronym(s["name"]) or ""] if norm(x or "")}:
            pairs.append((n, s["id"]))
    return sorted(pairs, key=lambda p: -len(p[0]))


def tag_role(tag: str, names: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(show id, tab) for a show's own tag, such as "Riptide Extras" -> ("riptide", "extras")."""
    t = norm(tag)
    for n, sid in names:
        if t == n:
            return sid, "episodes"
        if t.startswith(n + " "):
            rest = t[len(n) + 1:]
            if rest in TAG_SUFFIX_TABS:
                return sid, TAG_SUFFIX_TABS[rest]
            if SEASON_TAG_RE.fullmatch(rest):
                return sid, "episodes"
    return None


def tabs_from_tags(tag_names: list[str], names: list[tuple[str, str]]) -> tuple[str | None, list[str]]:
    """The show a post's tags put it in, and the tabs they put it in (empty when its tags don't say)."""
    show, tabs = None, []
    for tag in tag_names:
        role = tag_role(tag, names)
        if role:
            show = show or role[0]
            if role[0] == show and role[1] not in tabs:
                tabs.append(role[1])
        generic = GENERIC_TAG_TABS.get(norm(tag))
        if generic and generic not in tabs:
            tabs.append(generic)
    return show, tabs


def main_tab(tabs: list[str], media: bool) -> str:
    order = MAIN_TAB_ORDER[media]
    return min(tabs, key=lambda t: order.index(t) if t in order else len(order))


def shows_from_tags(db: Db) -> list[str]:
    """Shows the site tags but doesn't list in its Shows menu, such as Mythborne: a tag "<X> Extras" or "<X> Rolled"
    for an X that isn't a known show makes X a show. Returns the ids it added."""
    known = {n for n, _ in show_tag_names(db)}
    tags = {norm(t["name"]): t["name"] for t in db.q("SELECT name FROM tags") if t["name"]}
    added = []
    for t in sorted(tags):
        for suffix in ("extras", "extra", "rolled"):
            if t.endswith(" " + suffix):
                base = t[: -len(suffix) - 1].strip()
                if base and base not in known and base not in GENERIC_TAG_TABS:
                    name = tags.get(base) or tags[t][: len(tags[t]) - len(suffix) - 1].strip()
                    add_show(db, name, source="tags")
                    known.add(base)
                    added.append(slugify(name))
    return added


def add_show(db: Db, name: str, source: str = "manual", aliases: list[str] | None = None) -> str:
    """A show that isn't in the site's Shows menu. Its posts are found by its tags and title, like any show's."""
    sid = slugify(name)
    if not sid or sid == OTHER[0]:
        raise ValueError("a show needs a name")
    if not db.one("SELECT id FROM shows WHERE id=?", (sid,)):
        n = db.one("SELECT COUNT(*) AS n FROM shows WHERE sort >= 500 AND sort < 999")["n"]
        db.upsert("shows", "id", {"id": sid, "name": name.strip(), "sort": 500 + n, "aliases": aliases or [],
                                  "source": source})
    return sid


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


def episode_parts(title: str, tag_names: list[str] = ()) -> tuple[int | None, int | None]:
    """(season, episode) from a title. The season can also come from a tag such as "Prime Defenders Season 2"."""
    title = title or ""
    for rx in SEASON_EPISODE_RES:
        m = rx.search(title)
        if m:
            return int(m.group(1)), int(m.group(2))
    episode = next((int(m.group(1)) for rx in EPISODE_RES if (m := rx.search(title))), None)
    if episode is None:
        return None, None
    m = SEASON_RE.search(title)
    if m:
        return int(m.group(1) or m.group(2)), episode
    for tag in tag_names:
        m = re.search(r"\bseason\s*(\d{1,2})\b", tag or "", re.I)
        if m:
            return int(m.group(1)), episode
    return None, episode


def has_episode_marker(text: str) -> bool:
    return episode_parts(text)[1] is not None


def pick_tab(post: dict, show_id: str, tag_names: list[str]) -> str:
    t = norm(post["title"])
    tags = [norm(x) for x in tag_names]
    if "just rolled with it" in t or any("rolled" in x.split() for x in tags):
        return "aftershow"
    if "art scene" in t or "art" in tags or re.search(r"\bart\b", t) and post["path_kind"] not in MEDIA_KINDS:
        return "art"
    if post["path_kind"] in MEDIA_KINDS:
        if show_id != OTHER[0] and has_episode_marker(post["title"]) and not any("extra" in x for x in tags):
            return "episodes"
        return "extras"
    if post["path_kind"] == "posts" and not (post.get("body_text") or "").strip() and post.get("images"):
        return "art"
    return "posts"


def episode_number(title: str) -> int | None:
    return episode_parts(title)[1]


def base_title(title: str) -> str:
    """The title without its version label, for matching versions: "JRWI" in front of a show name is left out too."""
    return " ".join(w for w in norm(VERSION_LABEL_RE.sub(" ", title or "")).split() if w != "jrwi")


def is_audio_post(p: dict) -> bool:
    return p["path_kind"] == "audio" or bool(AUDIO_ONLY_RE.search(p["title"] or ""))


def classify_all(db: Db) -> None:
    """Assign show, tabs and episode number to every post, and group video/audio versions.

    As on the site, a post's tags decide: a show's plain tag puts it in Episodes, "<show> Extras" in Extras and
    "<show> Rolled" in Just Rolled With It, and a post with several is in each. Posts whose tags don't say are
    sorted by their title instead."""
    shows_from_tags(db)
    matchers = show_matchers(db)
    names_for_tags = show_tag_names(db)
    tag_names = {t["id"]: t["name"] for t in db.q("SELECT id, name FROM tags")}
    posts = db.q("SELECT id, title, path_kind, tag_ids, body_text, images, published, show_override, "
                 "tab_override, episode_override, season_override, pair_id, show_id, tab, tabs, episode, season "
                 "FROM posts")
    for p in posts:
        names = [tag_names.get(str(i), "") for i in (p["tag_ids"] or [])]
        tag_show, tag_tabs = tabs_from_tags(names, names_for_tags)
        show = p["show_override"] or tag_show or match_show(p["title"], names, matchers)
        if p["tab_override"]:
            tabs = [p["tab_override"]]
        elif tag_tabs:
            tabs = tag_tabs
        else:
            tabs = [pick_tab(p, show, names)]
        tab = main_tab(tabs, p["path_kind"] in MEDIA_KINDS)
        tabs = sorted(tabs, key=lambda t: [x for x, _ in TABS].index(t) if t in TAB_NAMES else 99)
        season, ep = episode_parts(p["title"], names)
        if p["episode_override"] is not None:
            ep = p["episode_override"]
        if p["season_override"] is not None:
            season = p["season_override"]
        if (show, tab, tabs, ep, season) != (p["show_id"], p["tab"], p["tabs"], p["episode"], p["season"]):
            db.update("posts", "id", p["id"], {"show_id": show, "tab": tab, "tabs": tabs, "episode": ep,
                                               "season": season})

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
    clean = VERSION_LABEL_RE.sub(" ", title or "")
    if tab != "episodes":
        return re.sub(r"\s+", " ", clean).strip() or title
    parts = [x.strip() for x in clean.split("|")]
    names = [norm(show_name), *[norm(a) for a in aliases]]
    keep = [x for x in parts if not (has_episode_marker(x) and any(n and n in norm(x) for n in names))]
    out = " | ".join(keep) if keep else clean
    out = out.strip().strip('"“”\'').strip()
    return re.sub(r"\s+", " ", out) or title


def jellyfin_target(db: Db, post: dict) -> tuple[str, int, int]:
    """(relative path without extension, season, episode) for a primary video post."""
    show = db.one("SELECT * FROM shows WHERE id=?", (post["show_id"] or "other",)) or {"name": "Other", "aliases": []}
    show_name = safe_filename(show["name"] if show["id"] != "other" else "Just Roll With It - Other", 80, keep_ext=False)
    if post["tab"] == "episodes" and post["episode"]:
        season, number = int(post.get("season") or 1), int(post["episode"])
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
    # Show art: the show's picture, or the JRWI logo when it has none, fitted whole into a 2:3 poster; the backdrop
    # is the show's picture or its newest thumbnail. A small record of where each picture came from lets another
    # one replace it later.
    record = folder / ".jrwi-art.json"
    try:
        made = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        made = {}
    art = show_art(db, show)
    wanted = {"poster.jpg": ("fit", art or site_logo(db)),
              "fanart.jpg": ("own", art or newest_thumbnail(db, show["id"]))}
    changed = False
    for name, (how, rel) in wanted.items():
        dest = folder / name
        key = f"{how}:{rel}" if rel else None
        if not key or not (paths.archive / rel).exists():
            continue
        if made.get(name) == key and dest.exists():
            continue
        if (to_poster if how == "fit" else to_jpeg)(paths.archive / rel, dest):
            made[name] = key
            changed = True
    if changed:
        record.write_text(json.dumps(made), encoding="utf-8")


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
