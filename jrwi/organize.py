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
# The site's tags that are topics rather than shows. Every other tag that isn't one of a show's own is a show the
# Shows menu leaves out (Mythborne, The Council, Black Rose Pirates, ...).
TOPIC_TAGS = {"youtube", "art", "announcement", "campaign notes", "one shot", "thumbnails", "music",
              "just rolled with it"}
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
EPISODE_RES = [re.compile(r"#\s*(\d{1,4})(?![.\d])(?!\s*(?:&|and)\s*\d)"),
               re.compile(r"\b(?:Episode|Ep)\.?\s*#?\s*(\d{1,4})(?![.\d])(?!\s*(?:&|and)\s*\d)", re.I),
               re.compile(r"\bCase\s*File\s*#?\s*(\d{1,4})\b(?!\s*&)", re.I),   # UPP: "[Case File 3]", not "1 & 2"
               re.compile(r"\[\s*E(\d{1,3})\s*\]", re.I)]                     # "[E1]"
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


def places_from_tags(tag_names: list[str], names: list[tuple[str, str]]) -> tuple[str | None, list[tuple[str, str]], list[str]]:
    """Where a post's tags put it, as on the site: every (show, tab) its show tags name, the show it belongs to (the
    one whose plain tag it has, else the first named), and the tabs topic tags add for that show."""
    places: list[tuple[str, str]] = []
    for tag in tag_names:
        role = tag_role(tag, names)
        if role and role not in places:
            places.append(role)
    main = next((s for s, t in places if t == "episodes"), places[0][0] if places else None)
    generic = []
    for tag in tag_names:
        t = GENERIC_TAG_TABS.get(norm(tag))
        if t and t not in generic:
            generic.append(t)
    return main, places, generic


def main_tab(tabs: list[str], media: bool) -> str:
    order = MAIN_TAB_ORDER[media]
    return min(tabs, key=lambda t: order.index(t) if t in order else len(order))


def not_shows(db: Db) -> set[str]:
    return TOPIC_TAGS | {norm(x) for x in (db.setting("not_shows") or [])}


def shows_from_tags(db: Db) -> list[str]:
    """Shows the site tags but doesn't list in its Shows menu, such as Mythborne or The Council: every tag that isn't
    one of a known show's own, and isn't a topic, is a show; "<X> Extras" or "<X> Rolled" make X one too. Tags the
    user marked as not a show are left alone. Returns the ids it added."""
    names = show_tag_names(db)
    skip = not_shows(db)
    tags = {norm(t["name"]): t["name"] for t in db.q("SELECT name FROM tags") if t["name"]}
    added = []
    for t in sorted(tags, key=len):
        if tag_role(tags[t], names):
            continue
        base, name = t, tags[t]
        for suffix in ("extras", "extra", "rolled"):
            if t.endswith(" " + suffix):
                base = t[: -len(suffix) - 1].strip()
                name = tags.get(base) or tags[t][: len(tags[t]) - len(suffix) - 1].strip()
        if base and base not in skip and not tag_role(name, names):
            sid = add_show(db, name, source="tags")
            names = show_tag_names(db)
            added.append(sid)
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


STOPWORDS = {"the", "a", "an", "of", "and", "ft", "feat", "part", "pt"}
NOISE_RE = re.compile(r"\b(just roll with it|just rolled with it|jrwi|patreon|early access|audio only|audio|video|"
                      r"finale|season \d+|s\d+e\d+|s\d+|episode \d+|ep \d+|case file \d+|e\d+|\d+)\b")


def name_key(title: str, show_names: list[str]) -> str:
    """An episode's own name, for telling a re-post of an episode from a new one: "Island of the Amalgam |
    Prime Defenders Season 1 Episode 2 (Video)" and "Island of Amalgam | Just Roll With It: Prime Defenders #2"
    both give "island amalgam"."""
    t = f" {norm(VERSION_LABEL_RE.sub(' ', title or ''))} "
    for n in sorted(show_names, key=len, reverse=True):
        if n:
            t = t.replace(f" {n} ", " ")
    t = NOISE_RE.sub(" ", t)
    return " ".join(w for w in t.split() if w not in STOPWORDS)


def names_match(a: str, b: str) -> bool:
    """Same episode name, allowing small differences. An empty name (an early-access post, say) matches any."""
    if not a or not b:
        return True
    if a == b or a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / max(1, len(wa | wb)) >= 0.6


def show_number(title: str, show_names: list[str]) -> int | None:
    """"Riptide 102 - Audio Early Access", "Sister Screwup | Riptide 121": a number straight after the show's name."""
    for n in sorted(show_names, key=len, reverse=True):
        if not n:
            continue
        name = r"\W+".join(map(re.escape, n.split()))
        m = re.search(rf"\b{name}\W{{0,3}}(\d{{1,4}})\b(?![.\d])(?!\s*(?:&|and|-)\s*\d)", title or "", re.I)
        if m:
            return int(m.group(1))
    return None


def season_name_of(title: str, show_names: list[str]) -> str:
    """What's left of a title without the show's name, the episode marker and the version label, in its own
    capitals: "Unwitness Protection Program - Diseased Residents [Case File 1]" gives "Diseased Residents"."""
    t = VERSION_LABEL_RE.sub(" ", title or "")
    for rx in [*SEASON_EPISODE_RES, *EPISODE_RES]:
        t = rx.sub(" ", t)
    t = re.sub(r"\b(?:Just Roll With It|JRWI)\b:?", " ", t, flags=re.I)
    for n in sorted(show_names, key=len, reverse=True):
        if n:
            t = re.sub(r"\b" + r"\W+".join(map(re.escape, n.split())) + r"\b", " ", t, flags=re.I)
    t = re.sub(r"[\[\]()|:#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -–—.,") or ""


def infer_seasons(posts: list[dict], show_names: list[str]) -> dict[int, int]:
    """Seasons for one show's episodes, in upload order. A season named in the title, a tag or by the user is used
    as it is. Otherwise a post that's another version of an earlier episode (same number and name) gets that
    episode's season, and an episode 1 after the current season already has episodes starts a new season."""
    out: dict[int, int] = {}
    current, history = 1, []   # (season, number, name key, date) of videos and audio
    # Same-day posts in episode order, so a #1 and #2 uploaded together don't look like a restart
    for p in sorted(posts, key=lambda p: ((p["published"] or "")[:10], p["episode"] or 0, p["id"])):
        num, key, day = p["episode"], name_key(p["title"], show_names), (p["published"] or "")[:10]
        media = p["path_kind"] in MEDIA_KINDS
        if p["explicit_season"] is not None:
            season = p["explicit_season"]
            current = max(current, season)
        elif not media or num is None:
            season = current
        elif same := next((h for h in reversed(history) if h[1] == num and names_match(h[2], key)), None):
            season = same[0]
        elif num == 1 and any(h[0] == current and (h[1] or 0) >= 2 and h[3] < day for h in history):
            current += 1
            season = current
        else:
            season = current
        if media:
            history.append((season, num, key, day))
        out[p["id"]] = season
    return out


def aftershow_seasons(rolled: list[dict], episodes: list[dict], seasons: dict[int, int],
                      named: dict[int, str]) -> dict[int, int]:
    """Seasons for a show's Just Rolled With It posts: the one their title names ("Season 2 Episode 1", or a season's
    name such as "Empty Graves"), else that of the episode with their number uploaded last before them ("#23 and
    #24"), else that of the show's last episode before them. Nothing when the show has no episodes."""
    timeline = sorted(((e["published"] or "")[:10], e["id"], seasons[e["id"]], e["episode"]) for e in episodes
                      if e["path_kind"] in MEDIA_KINDS and e["id"] in seasons)
    out: dict[int, int] = {}
    for r in rolled:
        day = (r["published"] or "")[:10]
        before = [t for t in timeline if t[0] <= day]
        name_hit = next((n for n, nm in named.items() if nm and f" {norm(nm)} " in f" {norm(r['title'])} "), None)
        if r["explicit_season"] is not None:
            out[r["id"]] = r["explicit_season"]
        elif name_hit is not None:
            out[r["id"]] = name_hit
        elif r["episode"] is not None and (same := [t for t in before if t[3] == r["episode"]]
                                           or [t for t in timeline if t[3] == r["episode"]][:1]):
            out[r["id"]] = same[-1][2]
        elif before or timeline:
            out[r["id"]] = (before or timeline)[-1 if before else 0][2]
    return out


def season_names(posts: list[dict], seasons: dict[int, int], show_names: list[str]) -> dict[int, str]:
    """A season's name, when all its numbered episodes share one besides their own number, as UPP's do."""
    found: dict[int, list[tuple[str, str]]] = {}
    for p in posts:
        if p["episode"] is not None and p["id"] in seasons:
            raw = season_name_of(p["title"], show_names)
            found.setdefault(seasons[p["id"]], []).append((norm(raw), raw))
    out = {}
    for season, names in found.items():
        keys = {k for k, _ in names}
        numbers = {p["episode"] for p in posts if seasons.get(p["id"]) == season and p["episode"] is not None}
        if len(keys) == 1 and "" not in keys and len(numbers) >= 2:
            out[season] = names[0][1]
    return out


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
    """Assign show, tabs, episode and season to every post, and group the versions of each episode.

    As on the site, a post's tags decide: a show's plain tag puts it in that show's Episodes, "<show> Extras" in its
    Extras and "<show> Rolled" in its Just Rolled With It, and a post with several, of one show or of several shows,
    is in each. Posts whose tags don't say are sorted by their title instead."""
    shows_from_tags(db)
    matchers = show_matchers(db)
    names_for_tags = show_tag_names(db)
    by_show: dict[str, list[str]] = {}
    for n, sid in names_for_tags:
        by_show.setdefault(sid, []).append(n)
    tag_names = {t["id"]: t["name"] for t in db.q("SELECT id, name FROM tags")}
    posts = db.q("SELECT id, title, path_kind, tag_ids, body_text, images, published, show_override, "
                 "tab_override, episode_override, season_override, pair_id, show_id, tab, tabs, places, episode, "
                 "season FROM posts")
    order = [x for x, _ in TABS]
    new: dict[int, dict] = {}
    for p in posts:
        names = [tag_names.get(str(i), "") for i in (p["tag_ids"] or [])]
        tag_show, places, generic = places_from_tags(names, names_for_tags)
        show = p["show_override"] or tag_show or match_show(p["title"], names, matchers)
        if p["tab_override"]:
            tabs = [p["tab_override"]]
        else:
            tabs = [t for s, t in places if s == show]
            if "just rolled with it" in norm(p["title"]) and "aftershow" not in generic:
                generic.append("aftershow")   # an aftershow tagged only with its show's plain tag
            tabs += [t for t in generic if t not in tabs]
            if not tabs:
                tabs = [pick_tab(p, show, names)]
        tab = main_tab(tabs, p["path_kind"] in MEDIA_KINDS)
        tabs = sorted(tabs, key=lambda t: order.index(t) if t in order else 99)
        flat = [f"{show}:{t}" for t in tabs] + [f"{s}:{t}" for s, t in places if s != show and f"{s}:{t}"]
        season, ep = episode_parts(p["title"], names)
        if ep is None:
            ep = show_number(p["title"], by_show.get(show, []))
        if p["episode_override"] is not None:
            ep = p["episode_override"]
        if p["season_override"] is not None:
            season = p["season_override"]
        new[p["id"]] = {"show_id": show, "tab": tab, "tabs": tabs, "places": list(dict.fromkeys(flat)),
                        "episode": ep, "explicit_season": season}

    # Seasons, per show, from its Episodes in upload order
    show_rows = {s["id"]: s for s in db.q("SELECT id, name, seasons FROM shows")}
    for sid in {n["show_id"] for n in new.values()}:
        eps = [{**p, **new[p["id"]]} for p in posts
               if new[p["id"]]["show_id"] == sid and "episodes" in new[p["id"]]["tabs"]]
        seasons = infer_seasons(eps, by_show.get(sid, []) + ["just roll with it", "jrwi"])
        for pid, season in seasons.items():
            new[pid]["season"] = season
        named = season_names([e for e in eps if e["path_kind"] in MEDIA_KINDS], seasons, by_show.get(sid, []))
        # The show's Just Rolled With It posts belong to the season of the episodes they talk about
        rolled = [{**p, **new[p["id"]]} for p in posts if new[p["id"]]["show_id"] == sid
                  and "aftershow" in new[p["id"]]["tabs"] and p["id"] not in seasons]
        for pid, season in aftershow_seasons(rolled, eps, seasons, named).items():
            new[pid]["season"] = season
        if sid in show_rows:
            listed = [{"season": n, "name": named.get(n)} for n in sorted(set(seasons.values()))]
            if listed != (show_rows[sid]["seasons"] or []):
                db.update("shows", "id", sid, {"seasons": listed})
    for p in posts:
        n = new[p["id"]]
        n.setdefault("season", n["explicit_season"])
        fields = {k: n[k] for k in ("show_id", "tab", "tabs", "places", "episode", "season")}
        if any(fields[k] != p[k] for k in fields):
            db.update("posts", "id", p["id"], fields)
        p.update(fields)

    # Group the versions of one episode: members-only video, YouTube embed, audio only. Episodes are grouped by show,
    # season and number when their names agree, however differently the versions are titled; other posts by title.
    def rank(p: dict) -> tuple:
        kind = "audio" if is_audio_post(p) else p["path_kind"]
        early = "early access" in norm(p["title"])
        return ({"videos": 0, "video_embeds": 1, "audio": 2}.get(kind, 3) + (3 if early else 0), p["published"] or "")

    new_pairs: dict[int, int | None] = {p["id"]: None for p in posts}
    grouped: set[int] = set()
    episodes: dict[tuple, list[dict]] = {}
    for p in posts:
        if p["path_kind"] in MEDIA_KINDS and p["tab"] == "episodes" and p["episode"] is not None:
            episodes.setdefault((p["show_id"], p["season"] or 1, p["episode"]), []).append(p)
    for (sid, _, _), members in episodes.items():
        clusters: list[tuple[str, list[dict]]] = []
        for p in sorted(members, key=lambda p: (p["published"] or "", p["id"])):
            key = name_key(p["title"], by_show.get(sid, []))
            for i, (ckey, group) in enumerate(clusters):
                if names_match(ckey, key):
                    group.append(p)
                    clusters[i] = (ckey or key, group)
                    break
            else:
                clusters.append((key, [p]))
        for _, group in clusters:
            group.sort(key=rank)
            for m in group[1:]:
                new_pairs[m["id"]] = group[0]["id"]
            grouped.update(m["id"] for m in group)
    by_title: dict[str, list[dict]] = {}
    for p in posts:
        if p["path_kind"] in MEDIA_KINDS and p["id"] not in grouped:
            by_title.setdefault(base_title(p["title"]), []).append(p)
    for members in by_title.values():
        if len(members) < 2:
            continue
        members.sort(key=rank)
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
    # Season names, such as UPP's "Diseased Residents", for the season folders Jellyfin shows
    for s in show.get("seasons") or []:
        season_dir = folder / f"Season {int(s['season']):02d}"
        if s.get("name") and season_dir.is_dir():
            sxml = ('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<season>\n'
                    f"  <title>Season {int(s['season'])}: {escape(s['name'])}</title>\n"
                    f"  <seasonnumber>{int(s['season'])}</seasonnumber>\n  <lockdata>true</lockdata>\n</season>\n")
            snfo = season_dir / "season.nfo"
            if not snfo.exists() or snfo.read_text(encoding="utf-8") != sxml:
                snfo.write_text(sxml, encoding="utf-8")
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
