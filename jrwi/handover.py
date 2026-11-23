"""Handing an episode's video over to TubeArchivist once it's on YouTube.

Riptide, and other shows at times, go up on the members site first as a members-only video, and on YouTube later.
The site then adds a post that embeds the YouTube video (dated like the first one) and takes the members-only post
down. Both end up in one item here, which then has an archived video and a YouTube version.

Once TubeArchivist has downloaded that YouTube video, keeping the archived copy too only doubles the space, so the
archived video is deleted and the item plays from YouTube (TubeArchivist) like any other YouTube-only episode. That
only happens when the two are the same video: TubeArchivist's copy must be within TOLERANCE seconds of the archived
one's length. Otherwise both are kept and the admin page says why, so the user can decide.

Before deleting, the two videos are lined up (as video and audio are) to find how much later the same moment is on
YouTube. Positions saved in the video's timeline and the audio's sync offset are moved by that much, so resuming
still lands in the same place.

What's recorded on the post with the archived video (posts.handover):
  status: "waiting" (TubeArchivist doesn't have it yet), "differs" (lengths don't match), "ready" (it could go,
          but deleting is turned off in Settings), "done" (deleted), or "keep" (the user keeps it)
  youtube_id, youtube_post, youtube_url, checked_at, archived_duration, ta_duration, and for "done": at, shift,
  shift_checked, freed (bytes)
  force: True when the user asked to delete it anyway
  yt_shift, yt_shift_checked, yt_shift_for, yt_shift_at (0.14.8): while the archived video is kept, how much later the
          same moment is in TubeArchivist's copy (youtube = archived + yt_shift), measured once per copy, so the
          YouTube post's transcript lines can be given in its own timeline. yt_shift is None when it couldn't be told.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Paths
from .db import Db, now
from .site.download import glob_escape
from .sync import find_offset, probe_duration
from .tubearchivist import TaError, TubeArchivist

log = logging.getLogger(__name__)

TOLERANCE = 10.0   # seconds the two lengths may differ by and still count as the same video
MEASURE_PER_RUN = 10   # gaps measured for kept archived videos per check (each reads about ten minutes of both)
STATUSES = {
    "waiting": "Waiting for TubeArchivist to download the YouTube version.",
    "differs": "TubeArchivist has the YouTube version, but its length differs, so the archived video is kept.",
    "ready": "TubeArchivist has the same video. Deleting archived videos is turned off in Settings, so it's kept.",
    "done": "The archived video was deleted because TubeArchivist has the same video from YouTube.",
    "keep": "You keep the archived video, even though it's on YouTube.",
}


def members_of(db: Db, primary: dict) -> list[dict]:
    return [primary] + db.q("SELECT * FROM posts WHERE pair_id=? ORDER BY id", (primary["id"],))


def youtube_version(members: list[dict]) -> tuple[dict, dict] | None:
    """(post, embed) of the YouTube video in an item, if it has one."""
    for m in members:
        for e in m["embeds"] or []:
            if e.get("kind") == "youtube" and e.get("youtube_id"):
                return m, e
    return None


def candidates(db: Db, paths: Paths) -> list[dict]:
    """Items with an archived video and a YouTube version, one entry per archived video."""
    out = []
    for post in db.q("SELECT * FROM posts WHERE media LIKE '%\"video\"%' ORDER BY published DESC, id DESC"):
        v = (post["media"] or {}).get("video") or {}
        if not v.get("path") or not (paths.media / v["path"]).is_file():
            continue
        primary = (db.post(post["pair_id"]) if post["pair_id"] else None) or post
        members = members_of(db, primary)
        found = youtube_version(members)
        if found:
            out.append({"post": post, "primary": primary, "members": members, "yt_post": found[0],
                        "youtube_id": found[1]["youtube_id"], "youtube_url": found[1]["url"]})
    return out


def remember(db: Db, post_id: int, old: dict | None, **fields) -> dict:
    h = {**(old or {}), **fields}
    db.update("posts", "id", post_id, {"handover": h})
    return h


def run(db: Db, paths: Paths, settings: dict, status=None, stop_event=None) -> dict:
    """Look at every item with both an archived video and a YouTube version. Returns counts by outcome."""
    ta = TubeArchivist.from_settings(settings)
    counts: dict[str, int] = {}
    found = candidates(db, paths)
    if found and status:
        status.begin("tubearchivist", total=len(found))
    measured = 0
    for n, c in enumerate(found):
        if stop_event is not None and stop_event.is_set():
            break
        post, h = c["post"], c["post"].get("handover") or {}
        if status:
            status.set(done=n, detail=f"{post['title'] or ''} · does TubeArchivist have it yet?")
        if h.get("status") == "keep":
            # Kept for good: only the gap to TubeArchivist's copy is measured, once, for the YouTube post's transcript
            if ta.configured and "yt_shift_for" not in h and measured < MEASURE_PER_RUN:
                try:
                    video = ta.video(c["youtube_id"])
                except TaError as e:
                    log.warning("TubeArchivist: %s", e)
                    video = None
                if video is not None:
                    youtube_shift(db, paths, ta, c, video, h)
                    measured += 1
            counts["keep"] = counts.get("keep", 0) + 1
            continue
        base = dict(youtube_id=c["youtube_id"], youtube_post=c["yt_post"]["id"], youtube_url=c["youtube_url"],
                    checked_at=now())
        if not ta.configured:
            remember(db, post["id"], h, status="waiting", note="TubeArchivist isn't connected (Settings)", **base)
            counts["waiting"] = counts.get("waiting", 0) + 1
            continue
        try:
            video = ta.video(c["youtube_id"])
        except TaError as e:
            db.set_setting("ta_status", {"ok": False, "message": str(e), "at": now()})
            log.warning("TubeArchivist: %s", e)
            break
        db.set_setting("ta_status", {"ok": True, "message": "reachable", "at": now()})
        if video is None:
            remember(db, post["id"], h, status="waiting", note=None, **base)
            counts["waiting"] = counts.get("waiting", 0) + 1
            continue
        v = post["media"]["video"]
        ours = v.get("duration") or probe_duration(paths.media / v["path"])
        theirs = TubeArchivist.duration(video)
        same = ours is not None and theirs is not None and abs(ours - theirs) <= TOLERANCE
        base.update(archived_duration=ours, ta_duration=theirs, note=None)
        keeps = (not same and not h.get("force")) or (not settings.get("handover", True) and not h.get("force"))
        if keeps and measured < MEASURE_PER_RUN and h.get("yt_shift_for") != shift_key(c, video, v):
            h = youtube_shift(db, paths, ta, c, video, h)
            measured += 1
        if not same and not h.get("force"):
            remember(db, post["id"], h, status="differs", **base)
            counts["differs"] = counts.get("differs", 0) + 1
            continue
        if not settings.get("handover", True) and not h.get("force"):
            remember(db, post["id"], h, status="ready", **base)
            counts["ready"] = counts.get("ready", 0) + 1
            continue
        hand_over(db, paths, ta, c, video, ours, theirs, base)
        counts["done"] = counts.get("done", 0) + 1
    return counts


def shift_key(c: dict, video: dict, v: dict) -> dict:
    """What a measured gap belongs to: the YouTube video, TubeArchivist's copy of it and the archived file."""
    return {"youtube_id": c["youtube_id"], "ta_duration": TubeArchivist.duration(video), "file_size": v.get("size")}


def youtube_shift(db: Db, paths: Paths, ta: TubeArchivist, c: dict, video: dict, h: dict) -> dict:
    """While the archived video is kept, how much later the same moment is in TubeArchivist's copy of the YouTube
    version (youtube = archived + yt_shift), lined up as the hand-over does, so the YouTube post's transcript can be
    given in its own timeline (the Tubedroid chat's suggestion 13, 0.14.8). A copy that can't be lined up with
    confidence is remembered as such (yt_shift None) and not measured again; one that couldn't be read is tried again
    at the next check. Returns the handover record."""
    post = c["post"]
    v = post["media"]["video"]
    url = ta.file_url(video)
    if not url:
        return h
    ours = v.get("duration") or probe_duration(paths.media / v["path"])
    try:
        r = find_offset(paths.media / v["path"], url, ours, TubeArchivist.duration(video), audio_headers=ta.headers)
    except Exception as e:
        log.warning("couldn't line up %s with TubeArchivist's copy: %s", post["id"], type(e).__name__)
        return h
    log.info("the YouTube version of %s is %s", post["id"],
             f"{r['offset']:+.2f} s from the archived video" if r["checked"] else "not clearly lined up with it")
    return remember(db, post["id"], h, yt_shift=round(float(r["offset"]), 2) if r["checked"] else None,
                    yt_shift_checked=bool(r["checked"]), yt_shift_for=shift_key(c, video, v), yt_shift_at=now())


def hand_over(db: Db, paths: Paths, ta: TubeArchivist, c: dict, video: dict, ours: float | None,
              theirs: float | None, base: dict) -> None:
    post, primary, members = c["post"], c["primary"], c["members"]
    v = post["media"]["video"]
    file = paths.media / v["path"]
    # How much later the same moment is in TubeArchivist's copy (youtube position = archived position + shift)
    shift, checked = 0.0, False
    url = ta.file_url(video)
    if url:
        try:
            r = find_offset(file, url, ours, theirs, audio_headers=ta.headers)
            if r["checked"]:
                shift, checked = float(r["offset"]), True
        except Exception as e:  # can't compare: the lengths matched, so assume they line up
            log.warning("couldn't line up %s with TubeArchivist's copy: %s", post["id"], type(e).__name__)
    ids = [m["id"] for m in members]
    marks = ",".join("?" * len(ids))
    with db.lock:
        # Positions saved in the video's timeline now belong to the YouTube video's
        if shift:
            db.x(f"UPDATE progress SET position = MAX(0, position + ?) WHERE timeline = 'video' AND post_id IN ({marks})",
                 [shift, *ids])
        # audio = archived + offset, so audio = youtube - shift + offset
        # (when the shift couldn't be measured, the next line-up compares the audio with TubeArchivist's copy instead)
        sync = primary.get("sync") or {}
        if checked and sync.get("checked") and sync.get("video_post") == post["id"]:
            new = {**sync, "offset": round(sync["offset"] - shift, 2), "video_post": c["yt_post"]["id"],
                   "checked": True, "at": now(), "via": "TubeArchivist"}
            new.pop("video_size", None)
            db.update("posts", "id", primary["id"], {"sync": new})
        freed = 0
        for side in file.parent.glob(glob_escape(file.stem) + "*"):
            if side.is_file():
                freed += side.stat().st_size
                side.unlink()
        media = {k: val for k, val in (post["media"] or {}).items() if k != "video"}
        remember(db, post["id"], post.get("handover"), status="done", at=now(), shift=round(shift, 2),
                 shift_checked=checked, freed=freed, force=None, **base)
        db.update("posts", "id", post["id"], {"media": media})
    db.event("video_handed_over", f"The archived video was deleted: TubeArchivist has it from YouTube ({c['youtube_id']})",
             primary["id"], primary["title"] or "")
    log.info("handed %s over to TubeArchivist (%s), freed %d bytes", post["id"], c["youtube_id"], freed)


def set_choice(db: Db, post_id: int, choice: str) -> None:
    """The admin page's choices: "keep" the archived video, "allow" handing it over again, "force" deleting it at the
    next check even though the lengths differ."""
    post = db.post(post_id)
    h = (post or {}).get("handover") or {}
    if not post or not h:
        raise ValueError("this post has no YouTube version to hand over to")
    if h.get("status") == "done" and choice != "keep":
        raise ValueError("the archived video is already deleted")
    if choice == "keep":
        remember(db, post_id, h, status="keep", force=None)
    elif choice == "allow":
        remember(db, post_id, h, status="waiting", force=None)
    elif choice == "force":
        remember(db, post_id, h, status=h.get("status") if h.get("status") != "keep" else "waiting", force=True)
    else:
        raise ValueError("unknown choice")


def is_done(post: dict) -> bool:
    return ((post or {}).get("handover") or {}).get("status") == "done"


def file_of(paths: Paths, post: dict) -> Path | None:
    v = ((post or {}).get("media") or {}).get("video") or {}
    return paths.media / v["path"] if v.get("path") else None
