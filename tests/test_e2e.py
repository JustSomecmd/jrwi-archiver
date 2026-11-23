"""End-to-end test against the mock site: one check, then the API and the Jellyfin folder.

Needs ffmpeg for the test media:  sh tests/make_media.sh && python -m pytest tests
"""

import json
import re
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
if not (HERE / "media" / "master.m3u8").exists():
    pytest.skip("run tests/make_media.sh first", allow_module_level=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
TMP = Path(tempfile.mkdtemp(prefix="jrwi-test-"))
os.environ.update(
    JRWI_DATA=str(TMP / "data"), JRWI_ARCHIVE=str(TMP / "archive"), JRWI_MEDIA=str(TMP / "media"),
    JRWI_SITE=f"http://127.0.0.1:{PORT}", JRWI_MUX_BASE=f"http://127.0.0.1:{PORT}/mux",
    JRWI_YT_THUMBS=f"http://127.0.0.1:{PORT}/imgproxy/yt", JRWI_GDOCS_BASE=f"http://127.0.0.1:{PORT}/gdocs",
    JRWI_DRIVE_DOWNLOAD=f"http://127.0.0.1:{PORT}/drive/download",
    JRWI_DRIVE_FOLDER_VIEW=f"http://127.0.0.1:{PORT}/drive/folderview",
    JRWI_EMAIL="fan@example.com", JRWI_PASSWORD="hunter2",
)

from fastapi.testclient import TestClient  # noqa: E402

from jrwi.web.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def app():
    proc = subprocess.Popen([sys.executable, str(HERE / "mock_site.py"), str(PORT)], stdout=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    application = create_app(start_scheduler=False)
    db = application.state.db
    db.set_setting("request_delay", 0)
    db.set_setting("google_docs", False)
    # Record how far each file download got, to check the progress bar has something to show
    status = application.state.archiver.status
    application.state.file_progress = seen = []
    original_set = status.set

    def recording_set(**kw):
        if kw.get("current") is not None:
            seen.append(kw["current"])
        original_set(**kw)

    status.set = recording_set
    result = application.state.archiver.run_check()
    status.set = original_set
    assert result["result"] == "ok", result
    yield application
    proc.terminate()


@pytest.fixture(scope="module")
def client(app):
    c = TestClient(app)
    c.headers["Authorization"] = "Token " + app.state.db.setting("api_token")
    return c


def test_pairs_and_tabs(client):
    eps = client.get("/api/shows/riptide/items?tab=episodes").json()["items"]
    by_ep = {i["episode"]: i for i in eps}
    assert by_ep[136]["video"]["jellyfin_path"].endswith("[jrwi-300].mp4")
    assert by_ep[136]["audio"]["post_id"] == 298
    assert by_ep[135]["video"]["youtube_id"] == "CzZ13VPQOcw"
    assert by_ep[135]["thumbnail"]   # YouTube-only: the video's thumbnail, archived
    tabs = [t["id"] for t in client.get("/api/shows/riptide").json()["tabs"]]
    assert tabs == ["episodes", "aftershow", "art"]


def test_audio_thumbnail_is_the_cover(app):
    """Audio posts show a blurred strip behind a square cover. The cover is the thumbnail."""
    files = app.state.db.post(298)["files"]
    thumbs = [k for k in files if k.startswith("thumb:")]
    assert len(thumbs) == 1 and "acover298" in thumbs[0]


def test_versions_stay_separate_posts(app, client):
    """Items group an episode's video and audio for the players; the archive keeps each post on its own."""
    item = client.get("/api/items/300").json()
    versions = {v["post_id"]: v for v in item["versions"]}
    assert versions[300]["version"] == "video" and versions[300]["main"]
    assert versions[298]["version"] == "audio" and not versions[298]["main"]
    # Item comments come from both posts, each saying which post it's on
    merged = client.get("/api/items/300/comments").json()["items"]
    assert {(c["author"]["name"], c["post_id"]) for c in merged} == {("Kim", 300), ("Pat", 298)}
    # Each post on its own: its own comments, likes and files, and flags to recognise it
    audio = client.get("/api/posts/298").json()
    assert audio["version"] == "audio" and audio["item_id"] == 300 and audio["paired"]
    assert audio["video"] is None and audio["audio"]["post_id"] == 298
    assert [c["author"]["name"] for c in client.get("/api/posts/298/comments").json()["items"]] == ["Pat"]
    assert [c["author"]["name"] for c in client.get("/api/posts/300/comments").json()["items"]] == ["Kim"]
    ids = [p["id"] for p in client.get("/api/posts?show=riptide&tab=episodes").json()["items"]]
    assert 298 in ids and 300 in ids
    assert 298 not in [i["id"] for i in client.get("/api/shows/riptide/items?tab=episodes").json()["items"]]
    # The web app shows each post on its own
    web = TestClient(app)
    if not app.state.db.setting("web_password_hash"):
        web.post("/setup", data={"password": "test-password", "password2": "test-password"})
    web.post("/login", data={"password": "test-password"})
    for path in ("/", "/feed", "/shows/riptide", "/items/298", "/items/300", "/admin/items/298", "/admin"):
        r = web.get(path)
        assert r.status_code == 200, path
    page = web.get("/items/298").text
    assert "Audio version" in page and "Pat" in page and "Kim" not in page
    # The feed shows each post's two most liked comments, and loads the rest on request
    feed = web.get("/feed").text
    zombie = feed[feed.index('data-comments="299"'):]
    zombie = zombie[:zombie.index("</article>")]
    assert zombie.index("Sam") < zombie.index("Jo") and "Lee" not in zombie and "Load replies" in zombie
    rest = web.get("/items/299/comments-html?csort=top&offset=2&limit=10")
    assert "Lee" in rest.text and rest.headers["X-More"] == "0"
    # Home is the feed, like the site's members page, with the shows as tiles
    home = web.get("/").text
    assert 'data-comments="299"' in home and 'class="tile" href="/shows/riptide"' in home
    newest = web.get("/items/299/comments-html?csort=new&offset=0&limit=2")
    assert newest.headers["X-More"] == "1"


def test_jellyfin_layout():
    media = Path(os.environ["JRWI_MEDIA"])
    assert (media / "Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-300].mp4").exists()
    assert (media / "Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-300].nfo").exists()
    assert (media / "Riptide/tvshow.nfo").exists()
    assert any(media.glob("Riptide/Season 00/*[[]jrwi-297[]].mp4"))


def test_show_pictures(app, client):
    """A show page's own share picture is the show's picture. Pages that only carry the site-wide one get none, and
    the JRWI logo is shown instead. An override set on the admin page stays until it's turned off."""
    shows = {s["id"]: s for s in client.get("/api/shows?include_empty=true").json()}
    rip = shows["riptide"]
    assert rip["poster"] and rip["poster"] == rip["banner"] == rip["image"]
    db, archiver = app.state.db, app.state.archiver
    logo = "/files/" + db.setting("site_logo")
    for sid in ("unwitness-protection-program", "miller-troop-family-vacation"):
        assert shows[sid]["poster"] is None and shows[sid]["banner"] is None and shows[sid]["image"] == logo, sid
    assert len(db.setting("shared_show_art")) >= 2   # remembered, so a page saved again can't bring it back
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    assert 'src="' + logo + '"' in web.get("/").text   # Home's tile for a show without a picture
    page = web.get("/admin/shows/riptide").text
    assert "own picture on its page on the site" in page and "Override on" not in page
    row = db.one("SELECT * FROM shows WHERE id='riptide'")
    archive = archiver.paths.archive
    other = next(o for o in row["art_options"] if o != row["art"])   # the picture further down its page
    assert web.post("/admin/shows/riptide/art", data={"choice": other}).status_code == 200
    row = db.one("SELECT * FROM shows WHERE id='riptide'")
    # The picked picture is copied into the show's own override folder, so clearing a post can't take it away
    assert row["art_source"] == "override" and row["art"].startswith("shows/riptide/override/")
    assert (archive / row["art"]).read_bytes() == (archive / other).read_bytes()
    picked = row["art"]
    archiver.reparse()                                                # pages read again: the override stays
    from jrwi import organize
    organize.drop_shared_show_art(db)
    row = db.one("SELECT * FROM shows WHERE id='riptide'")
    assert (row["art"], row["art_source"]) == (picked, "override")
    assert client.get("/api/shows/riptide").json()["image"] == "/files/" + picked
    page = web.get("/admin/shows/riptide").text
    assert "Override on" in page and "Turn off the override" in page
    # Upload your own picture: a real picture is kept, anything else is turned away
    png = TMP / "own.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x300", "-frames:v", "1",
                    str(png)], check=True)
    r = web.post("/admin/shows/riptide/art/upload", files={"picture": ("own.png", png.read_bytes(), "image/png")})
    assert r.status_code == 200 and "Override on" in r.text
    row = db.one("SELECT * FROM shows WHERE id='riptide'")
    uploaded = row["art"]
    assert uploaded.endswith(".png") and uploaded.startswith("shows/riptide/override/") and uploaded != picked
    r = web.post("/admin/shows/riptide/art/upload", files={"picture": ("notes.png", b"not a picture", "image/png")})
    assert "isn&#39;t a JPEG, PNG, GIF or WebP" in r.text
    assert db.one("SELECT art FROM shows WHERE id='riptide'")["art"] == uploaded
    # Pictures from the show's posts, and any picture in the archive by its address
    page = web.get("/admin/shows/riptide").text
    assert "From the show's posts" in page and "Your pictures" in page
    thumb = organize.post_pictures(db, archive, "riptide")[0]["rel"]
    assert thumb.startswith("posts/")
    web.post("/admin/shows/riptide/art", data={"choice": f"http://box:8090/files/{thumb}"})
    row = db.one("SELECT * FROM shows WHERE id='riptide'")
    assert row["art_source"] == "override" and (archive / row["art"]).read_bytes() == (archive / thumb).read_bytes()
    for bad in ("shows/../../etc/passwd", "/files/../../../etc/passwd", "pages/home.html.gz", "nothing/here.jpg"):
        r = web.post("/admin/shows/riptide/art", data={"choice": bad})
        assert r.status_code == 200 and 'class="error"' in r.text, bad
        assert db.one("SELECT art FROM shows WHERE id='riptide'")["art"] == row["art"], bad
    # Saved pictures not in use can be deleted; the one in use can't
    assert 'class="error"' in web.post("/admin/shows/riptide/art/delete", data={"file": row["art"]}).text
    assert 'class="error"' in web.post("/admin/shows/riptide/art/delete", data={"file": other}).text  # not a saved one
    web.post("/admin/shows/riptide/art/delete", data={"file": uploaded})
    assert not (archive / uploaded).exists() and (archive / row["art"]).exists()
    web.post("/admin/shows/riptide/art", data={"choice": "/files/" + picked})   # back to the page picture's copy
    # Jellyfin: the picture fitted whole into a 2:3 poster; the JRWI logo when a show has none
    folder = Path(os.environ["JRWI_MEDIA"]) / "Riptide"
    made = json.loads((folder / ".jrwi-art.json").read_text())
    assert made["poster.jpg"] == "fit:" + picked and made["fanart.jpg"] == "own:" + picked
    web.post("/admin/shows/riptide/art", data={"choice": "logo"})
    assert db.one("SELECT art, art_source FROM shows WHERE id='riptide'") == {"art": None, "art_source": "override"}
    made = json.loads((folder / ".jrwi-art.json").read_text())
    assert made["poster.jpg"] == "fit:" + db.setting("site_logo") and made["fanart.jpg"].startswith("own:posts/")
    size = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                           str(folder / "poster.jpg")], capture_output=True, text=True).stdout.strip()
    assert size == "1000,1500"
    archiver.reparse()                                                # the logo override stays too
    assert db.one("SELECT art, art_source FROM shows WHERE id='riptide'") == {"art": None, "art_source": "override"}
    r = web.post("/admin/shows/riptide/art", data={"choice": "auto"})   # override off: the page's own share picture
    assert "Override off" in r.text
    row = db.one("SELECT * FROM shows WHERE id='riptide'")
    assert (row["art"], row["art_source"]) == (row["poster"], "page")
    organize.refresh_jellyfin(db, archiver.paths)


def test_post_with_attachment_and_comments(client):
    item = client.get("/api/items/299").json()
    assert [a["name"] for a in item["attachments"]] == ["Zombie Character Sheets.pdf"]
    assert len(item["images"]) == 1
    comments = client.get("/api/items/299/comments").json()
    assert comments["total"] == 3  # two on the first page, one more behind "View more comments"
    first = comments["items"][0]
    assert first["author"]["name"] == "Sam" and first["like_count"] == 4
    assert first["body_text"] == "Love these rules!"
    assert first["replies"][0]["body_text"] == "Same here."
    # A reply's date, shown twice over in its markup, read once
    assert first["replies"][0]["date_text"] == "2d" and first["replies"][0]["date"]
    assert first["author"]["avatar"].startswith("/files/avatars/") and first["author"]["badges"] == []
    # Member IDs from each comment's Report form; a reply has its own, and the user's own comment has none
    assert first["author"]["id"] == "81000001" and first["replies"][0]["author"]["id"] == "81000004"
    assert next(c for c in comments["items"] if c["author"]["name"] == "Lee")["author"]["id"] is None
    # No profile picture: no avatar, and the badges stay badges
    lee = next(c for c in comments["items"] if c["author"]["name"] == "Lee")
    assert lee["author"]["avatar"] is None
    badges = lee["author"]["badges"]
    assert [(b["name"], b["icon"]) for b in badges] == [("4-month member", "🔥"), ("JRWiener", None)]
    assert badges[0]["image"] is None and badges[1]["image"].startswith("/files/badges/")
    assert client.get(badges[1]["image"]).status_code == 200
    pinned = client.get("/api/items/300/comments").json()["items"][0]
    assert pinned["pinned"] and pinned["author"]["name"] == "Kim" and pinned["like_count"] == 327


def test_comment_member_ids():
    """The member ID is in a comment's Report form. A reply inside a comment has its own, which mustn't be taken
    for the comment's; the user's own comments have no Report form."""
    from jrwi.site.parse import parse_comments

    def report(sid):
        return ('<div class="modal"><form action="/supporters/reported_supporters" method="post">'
                f'<input type="hidden" name="reported_supporter_id" value="{sid}"></form></div>')

    def frame(cid, name, inner="", sid=None):
        return (f'<turbo-frame id="comment_{cid}"><div class="comment"><div class="comment__main">'
                f'<div data-testid="Post.CommentAuthor">{name}</div>{report(sid) if sid else ""}'
                f'<div class="comment__body"><p>Hi from {name}</p></div>'
                f'<turbo-frame id="comment_{cid}_replies">{inner}</turbo-frame></div></div></turbo-frame>')

    page = ('<turbo-stream action="append" target="post_1_comments"><template>'
            + frame(10, "Mine", inner=frame(11, "Replier", sid="555")) + frame(12, "Other", sid="777")
            + '</template></turbo-stream>')
    got = {c["author_name"]: c["author_id"] for c in parse_comments([(page, None)], "https://jrwishow.com/supporters")}
    assert got == {"Mine": None, "Replier": "555", "Other": "777"}


def test_card_details(client):
    item = client.get("/api/items/300").json()
    assert item["date"].startswith("2026-11-07")  # not a commenter's join date
    assert item["like_count"] == 469 and item["comment_count"] == 3
    assert item["tags"] == ["RIPTIDE"]
    assert item["thumbnail"]
    assert client.get("/api/items/299").json()["body_text"].startswith("Hi, scouts!")


def test_audio_progress_and_range(client):
    r = client.get("/api/audio/298", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10
    client.put("/api/progress/298", json={"position": 12.5})
    assert client.get("/api/progress/298").json()["position"] == 12.5


def test_video_and_audio_line_up(app, client):
    """The test video's soundtrack and the audio version are the same noise, the audio starting 2 s in."""
    item = client.get("/api/items/300").json()
    assert item["sync"]["checked"] and abs(item["sync"]["offset"] + 2.0) < 0.1
    assert abs(item["video"]["duration"] - 20) < 0.5 and abs(item["audio"]["duration"] - 20) < 0.5
    # A YouTube video can't be compared with the audio: no offset, and the apps know it wasn't checked
    assert client.get("/api/items/289").json()["sync"] == {"offset": 0.0, "checked": False}
    # Posts on their own have one part, so nothing to line up
    assert client.get("/api/posts/300").json()["sync"] is None
    # Lined-up pairs aren't measured again while their files stay the same
    at = app.state.db.post(300)["sync"]["at"]
    app.state.archiver.line_up()
    assert app.state.db.post(300)["sync"]["at"] == at


def test_watched_near_the_end_is_the_archives_setting(app, client):
    db = app.state.db
    db.x("DELETE FROM progress WHERE post_id IN (298, 300)")
    ping = client.get("/api/ping").json()
    assert ping["watched_left"] == 120.0 and "watched" in ping["features"]
    put = lambda body: client.put("/api/progress/300", json=body).json()["finished"]
    # A three-hour version: watched with two minutes left, not before
    assert put({"position": 10600, "duration": 10800}) is False
    assert put({"position": 10690, "duration": 10800}) is True
    # Going back to earlier unmarks it, as finished: false always has
    assert put({"position": 600, "duration": 10800}) is False
    # A short one is marked in its last 5%: 1 second of this 20-second test file, whose length the archive knows
    assert put({"position": 18.5}) is False
    assert put({"position": 19.5}) is True
    # The app's own say still decides: a watched button, and apps written before v0.22
    assert put({"position": 10790, "duration": 10800, "finished": False}) is False
    assert put({"position": 5, "duration": 10800, "finished": True}) is True
    # 0 means at the very end
    db.set_setting("watched_minutes_left", 0)
    assert client.get("/api/ping").json()["watched_left"] == 0.0
    assert put({"position": 10790, "duration": 10800}) is False
    assert put({"position": 10799.5, "duration": 10800}) is True
    db.set_setting("watched_minutes_left", 2)
    db.x("DELETE FROM progress WHERE post_id IN (298, 300)")


def test_progress_is_shared_between_video_and_audio(app, client):
    db = app.state.db
    offset = client.get("/api/items/300").json()["sync"]["offset"]
    db.x("DELETE FROM progress WHERE post_id IN (298, 300)")
    r = client.put("/api/progress/300", json={"position": 10, "duration": 20})
    assert r.json()["post_id"] == 300 and r.json()["position"] == 10
    audio = client.get("/api/progress/298").json()
    assert audio["post_id"] == 298 and abs(audio["position"] - (10 + offset)) < 0.01
    client.put("/api/progress/298", json={"position": 5})
    assert abs(client.get("/api/progress/300").json()["position"] - (5 - offset)) < 0.01
    # One place per episode, kept under the item's main post
    assert [r["post_id"] for r in db.q("SELECT post_id FROM progress WHERE post_id IN (298, 300)")] == [300]
    client.put("/api/progress/298", json={"position": 19.9, "finished": True})
    video = client.get("/api/progress/300").json()
    assert video["finished"] and video["position"] <= video["duration"]
    # Saved before v0.9, under the audio post: read in the audio's timeline, then moved on the next save
    db.x("DELETE FROM progress WHERE post_id IN (298, 300)")
    db.upsert("progress", "post_id", {"post_id": 298, "position": 6.0, "duration": 20, "finished": False,
                                      "updated_at": "2026-11-01T00:00:00+00:00"})
    assert abs(client.get("/api/progress/300").json()["position"] - (6 - offset)) < 0.01
    assert client.get("/api/progress/298").json()["position"] == 6.0
    client.put("/api/progress/300", json={"position": 9})
    assert [r["post_id"] for r in db.q("SELECT post_id FROM progress WHERE post_id IN (298, 300)")] == [300]
    # The post page starts the video where you are
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    page = web.get("/items/300").text
    assert 'data-progress="300"' in page and 'data-start="9.0"' in page


def test_youtube_lookup_and_progress_in_lists(client):
    """Contract v0.10: find an item by its YouTube ID, and see progress in lists without asking per item."""
    found = client.get("/api/items?youtube_id=CzZ13VPQOcw").json()
    assert [i["id"] for i in found["items"]] == [289] and found["items"][0]["video"]["youtube_id"] == "CzZ13VPQOcw"
    assert client.get("/api/items?youtube_id=nothing123").json()["total"] == 0
    assert [p["id"] for p in client.get("/api/posts?youtube_id=CzZ13VPQOcw").json()["items"]] == [289]
    eps = {i["episode"]: i for i in client.get("/api/shows/riptide/items?tab=episodes").json()["items"]}
    assert eps[136]["progress"]["post_id"] == 300 and eps[136]["progress"]["position"] == 9.0
    assert eps[135]["progress"] is None   # never played
    assert client.get("/api/items/298").json()["progress"]["position"] == 9.0   # the item's main post's timeline
    latest = {i["id"]: i for i in client.get("/api/latest").json()}
    assert latest[300]["progress"]["position"] == 9.0
    # Contract v0.17: single posts have it too, each in its own timeline
    assert client.get("/api/posts/300").json()["progress"]["position"] == 9.0
    posts = {p["id"]: p for p in client.get("/api/posts?page_size=200").json()["items"]}
    assert posts[298]["progress"]["post_id"] == 298 and posts[300]["progress"]["post_id"] == 300
    assert posts[299]["progress"] is None   # no video or audio


def test_locked_hidden_and_auth(app, client):
    ids = [i["id"] for i in client.get("/api/items").json()["items"]]
    assert 296 not in ids
    assert TestClient(app).get("/api/status").status_code == 401


def test_web_pages_need_login(app):
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).status_code == 303


def test_download_progress_is_reported(app):
    seen = app.state.file_progress
    assert seen and all(0 <= f <= 1 for f in seen)


def test_reparse_fixes_old_archives(app, client):
    """An older version that missed an attachment and saved a badge as a profile picture: re-reading the saved
    pages corrects the comments, removes the stray picture, and the next check fetches only what was missed."""
    from jrwi.site.parse import PARSER_VERSION

    archiver, db = app.state.archiver, app.state.db
    post = db.post(299)
    key, rel = next((k, v) for k, v in post["files"].items() if v.endswith("Zombie Character Sheets.pdf"))
    (archiver.paths.archive / rel).unlink()
    files = {k: v for k, v in post["files"].items() if k != key}
    db.update("posts", "id", 299, {"files": files, "attachments": []})
    stray = archiver.paths.archive / "avatars" / "0123456789abcdef.webp"
    stray.write_bytes(b"old badge")
    db.x("UPDATE comments SET author_avatar=? WHERE author_name='Lee'", (archiver.rel(stray),))
    # The audio post's thumbnail as an older version saved it: the blurred strip
    audio = db.post(298)
    (archiver.paths.archive / audio["thumbnail"]).unlink()
    thumb_file = archiver.paths.archive / "posts" / "298" / "thumbnail.webp"   # older versions' file name
    thumb_file.write_bytes(b"blurred strip")
    old_rel = str(thumb_file.relative_to(archiver.paths.archive))
    old_files = {("thumb:old-blurred-strip" if k.startswith("thumb:") else k): (old_rel if k.startswith("thumb:") else v)
                 for k, v in audio["files"].items()}
    db.update("posts", "id", 298, {"files": old_files, "thumbnail": old_rel})
    db.x("UPDATE comments SET author_id=NULL")   # saved before 0.9.8, which reads member IDs
    db.set_setting("parser_version", 1)

    result = archiver.run_reparse()
    assert result["to_fetch"] == 1 and result["removed"] == 1 and not result["stopped"]
    audio = db.post(298)   # the cover is fetched during the re-read, under a new name
    assert "images" not in (audio["skipped"] or [])
    assert [k for k in audio["files"] if k.startswith("thumb:")][0].find("acover298") > 0
    assert audio["thumbnail"] != old_rel and not thumb_file.exists()
    assert (archiver.paths.archive / audio["thumbnail"]).read_bytes() != b"blurred strip"
    assert not stray.exists()
    assert "attachments" in db.post(299)["skipped"]
    assert db.setting("parser_version") == PARSER_VERSION
    lee = next(c for c in client.get("/api/items/299/comments").json()["items"] if c["author"]["name"] == "Lee")
    assert lee["author"]["avatar"] is None
    assert db.one("SELECT author_id FROM comments WHERE author_name='Sam'")["author_id"] == "81000001"

    assert archiver.run_check()["result"] == "ok"
    post = db.post(299)
    assert [a["name"] for a in post["attachments"]] == ["Zombie Character Sheets.pdf"]
    assert "attachments" not in (post["skipped"] or [])


def test_google_drive_files(app, client):
    db, archiver = app.state.db, app.state.archiver
    assert "gdocs" in (db.post(290)["skipped"] or [])   # Google files are off in these tests until now
    db.set_setting("google_docs", True)
    try:
        assert archiver.run_check()["result"] == "ok"
    finally:
        db.set_setting("google_docs", False)
    post = db.post(290)
    docs = {d["name"]: d for d in post["documents"]}
    assert {"Map.pdf", "Big Map.pdf"} <= set(docs), docs
    assert (archiver.paths.archive / docs["Big Map.pdf"]["url"]).read_bytes().startswith(b"%PDF")
    assert docs["Map.pdf"]["source"].startswith("https://drive.google.com/file/d/")
    assert not post["problems"] and "gdocs" not in (post["skipped"] or [])
    shown = client.get("/api/posts/290").json()["documents"]
    assert {d["name"] for d in shown} >= {"Map.pdf", "Big Map.pdf"}


def test_drive_folders(app, client):
    """A shared Drive folder a post links to: its files, Google Docs and folders are saved, each with the folder
    it's in. A folder that isn't shared is noted, and a Google Form, which can't be a file, is left out. Saved by
    the check test_google_drive_files runs with Google files on."""
    archiver = app.state.archiver
    post = app.state.db.post(287)
    docs = {d["name"]: d for d in post["documents"]}
    assert docs["Handout.pdf"]["folder"] == "Campaign Maps" and docs["Map.pdf"]["folder"] == "Campaign Maps"
    assert docs["Old Map.pdf"]["folder"] == "Campaign Maps/Old maps"   # its link needed the resource key
    notes = next(n for n in docs if n.startswith("Session notes"))
    assert notes.endswith(".pdf") and docs[notes]["folder"] == "Campaign Maps"
    assert any("couldn't be read" in p and "1PrivatePrivatePrivatePriv01" in p for p in post["problems"])
    record = json.loads((archiver.paths.archive / post["files"]["gdrive_folder:1FolderCampaignMapsFolder0001"]).read_text())
    assert record["name"] == "Campaign Maps" and len(record["files"]) == 5
    assert next(f for f in record["files"] if f["name"] == "Sign-up form")["skipped"].startswith("a Google forms")
    # The API and the inspector say which folder each file is in; the folder link no longer shows as not saved
    full = client.get("/api/posts/287").json()
    assert {d["name"]: d["folder"] for d in full["documents"]}["Old Map.pdf"] == "Campaign Maps/Old maps"
    files = client.get("/api/posts/287/files").json()["files"]
    assert next(f for f in files if f["name"] == "Handout.pdf")["folder"] == "Campaign Maps"
    links = [f["source"] for f in files if f["role"] == "link"]
    assert links == ["https://drive.google.com/drive/folders/1PrivatePrivatePrivatePriv01"]
    assert all("folder" in f for f in files)
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    assert "In the Drive folder Campaign Maps/Old maps" in web.get("/items/287/files").text


def test_download_a_post_again(app, client):
    archiver, db = app.state.archiver, app.state.db
    post = db.post(299)
    rel = next(a["url"] for a in post["attachments"] if a["name"] == "Zombie Character Sheets.pdf")
    before = (archiver.paths.archive / rel).stat().st_mtime_ns
    events_before = db.one("SELECT COUNT(*) AS n FROM events WHERE type='new_item'")["n"]
    db.update("posts", "id", 299, {"refetch": "files"})
    assert archiver.run_check()["result"] == "ok"
    post = db.post(299)
    assert post["refetch"] is None and post["status"] == "complete"
    assert (archiver.paths.archive / rel).stat().st_mtime_ns != before   # deleted and downloaded again
    assert len(client.get("/api/items/299/comments").json()["items"]) == 3
    assert db.one("SELECT COUNT(*) AS n FROM events WHERE type='new_item'")["n"] == events_before


def test_seasons_and_other_episode_formats(app):
    """Titles carried over from Patreon number episodes differently and label versions "(Video)" and "(Audio)"."""
    from jrwi import organize
    db = app.state.db
    rows = [
        (9001, "videos", "Dropouts | Riptide Season 2 Episode 1 (Video)", "2021-10-06"),
        (9002, "audio", "Dropouts | Riptide Season 2 Episode 1 (Audio)", "2021-10-06"),
        (9003, "videos", "Fish and Chips | Riptide Episode 16", "2021-05-01"),
        (9004, "videos", "Riptide Season 2 Trailer", "2021-10-06"),
    ]
    try:
        for pid, kind, title, day in rows:
            db.upsert("posts", "id", {"id": pid, "url": f"https://example.invalid/{pid}", "path_kind": kind,
                                      "title": title, "published": day + "T00:00:00+00:00", "status": "complete"})
        organize.classify_all(db)
        got = {p["id"]: p for p in db.q("SELECT * FROM posts WHERE id >= 9001")}
        assert (got[9001]["tab"], got[9001]["season"], got[9001]["episode"]) == ("episodes", 2, 1)
        assert got[9002]["pair_id"] == 9001                    # "(Video)" and "(Audio)" are versions of one episode
        assert (got[9003]["tab"], got[9003]["season"], got[9003]["episode"]) == ("episodes", 1, 16)
        assert got[9004]["tab"] == "extras"                     # no episode number
        rel, season, number = organize.jellyfin_target(db, got[9001])
        assert (season, number) == (2, 1) and rel.startswith("Riptide/Season 02/Riptide - S02E01 - Dropouts [jrwi-9001]")
        assert organize.jellyfin_target(db, got[9003])[1:] == (1, 16)
    finally:
        db.x("DELETE FROM posts WHERE id >= 9001")
        organize.classify_all(db)


def test_progress_made_offline_arrives_later(client):
    """An app sends progress made offline with the time it was made; newer progress on the server wins."""
    from datetime import datetime, timedelta, timezone
    current = client.put("/api/progress/300", json={"position": 12}).json()
    late = client.put("/api/progress/300", json={"position": 4, "updated_at": "2026-01-01T10:00:00Z"}).json()
    assert late["position"] == 12 and late["updated_at"] == current["updated_at"]   # older: not kept
    fresh = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    kept = client.put("/api/progress/300", json={"position": 5, "updated_at": fresh}).json()
    assert kept["position"] == 5
    future = client.put("/api/progress/300", json={"position": 6, "updated_at": "2099-01-01T00:00:00+00:00"}).json()
    assert future["position"] == 6 and future["updated_at"] < "2099"              # a clock that's ahead isn't trusted
    assert client.put("/api/progress/300", json={"position": 1, "updated_at": "yesterday"}).status_code == 400
    assert client.put("/api/progress/300", json={"position": 1, "updated_at": "2026-01-01T10:00:00"}).status_code == 400


def test_tabs_follow_the_sites_tags(app, client):
    """As on the site: a show's plain tag is its Episodes, "<show> Extras" its Extras, and a post with both is in
    both. Initials count ("UPP Extras"), and shows the site only tags (Mythborne) become shows."""
    from jrwi import organize
    db = app.state.db
    tags = {"9201": "Riptide Extras", "9202": "UPP Extras", "9203": "Mythborne", "9204": "Mythborne Extras",
            "9205": "Just Rolled With It", "9206": "Monster Control Service"}
    rows = [(9101, "videos", "Riptide Q&A #50", ["101", "9201"]),
            (9102, "videos", "Behind the scenes", ["9202"]),
            (9103, "videos", "The Start | Mythborne #1", ["9203"]),
            (9104, "posts", "Mythborne maps", ["9204"]),
            (9105, "videos", "Riptide special", ["9205", "101"])]
    try:
        for tid, name in tags.items():
            db.upsert("tags", "id", {"id": tid, "name": name, "count": 1})
        for pid, kind, title, tag_ids in rows:
            db.upsert("posts", "id", {"id": pid, "url": f"https://example.invalid/{pid}", "path_kind": kind,
                                      "title": title, "tag_ids": tag_ids, "body_text": "Text",
                                      "published": "2021-05-01T00:00:00+00:00", "status": "complete"})
        organize.classify_all(db)
        got = {p["id"]: p for p in db.q("SELECT * FROM posts WHERE id >= 9101")}
        assert (got[9101]["show_id"], got[9101]["tabs"], got[9101]["tab"]) == ("riptide", ["episodes", "extras"], "extras")
        assert (got[9102]["show_id"], got[9102]["tabs"]) == ("unwitness-protection-program", ["extras"])
        assert db.one("SELECT source FROM shows WHERE id='mythborne'")["source"] == "tags"
        assert (got[9103]["show_id"], got[9103]["tabs"], got[9103]["episode"]) == ("mythborne", ["episodes"], 1)
        assert (got[9104]["show_id"], got[9104]["tabs"]) == ("mythborne", ["extras"])
        assert (got[9105]["tabs"], got[9105]["tab"]) == (["episodes", "aftershow"], "aftershow")
        in_tab = lambda tab: {i["id"] for i in client.get(f"/api/shows/riptide/items?tab={tab}&page_size=200").json()["items"]}
        assert 9101 in in_tab("episodes") and 9101 in in_tab("extras") and 9105 in in_tab("aftershow")
        assert client.get("/api/items/9101").json()["tabs"] == ["episodes", "extras"]
        counts = {t["id"]: t["count"] for t in client.get("/api/shows/mythborne").json()["tabs"]}
        assert counts == {"episodes": 1, "extras": 1}
        # Adding a show by hand, for one the site has no tag for; and tags no show matches are listed to pick from
        web = TestClient(app)
        web.post("/login", data={"password": "test-password"})
        # Tags that aren't one of a show's own become shows (the site's menu leaves some out); "isn't a show" undoes it
        assert db.one("SELECT source FROM shows WHERE id='monster-control-service'")["source"] == "tags"
        web.post("/admin/shows/monster-control-service/remove")
        assert not db.one("SELECT id FROM shows WHERE id='monster-control-service'")
        loose = web.get("/admin/shows").text.split("Tags no show matches")[1].split("<table>")[1]
        assert "Monster Control Service" in loose and "Mythborne" not in loose
        web.post("/admin/shows/add", data={"name": "Monster Control Service", "aliases": ""})
        assert db.one("SELECT source FROM shows WHERE id='monster-control-service'")["source"] == "manual"
        web.post("/admin/shows/add", data={"name": "Tales from the Midnight Rose", "aliases": ""})
        assert db.one("SELECT source FROM shows WHERE id='tales-from-the-midnight-rose'")["source"] == "manual"
        web.post("/admin/shows/tales-from-the-midnight-rose/remove")
        assert not db.one("SELECT id FROM shows WHERE id='tales-from-the-midnight-rose'")
    finally:
        db.x("DELETE FROM posts WHERE id >= 9101")
        db.x("DELETE FROM tags WHERE id >= '9201' AND id <= '9206'")
        db.x("DELETE FROM shows WHERE id IN ('mythborne', 'monster-control-service')")
        db.set_setting("not_shows", [])
        organize.classify_all(db)


def test_seasons_from_restarting_numbers(app, client):
    """Unwitness Protection Program numbers its episodes "Case File 1, 2, 3", then starts again at 1 for its second
    season. A later re-post of an old episode (same number and name) doesn't start a season. Posts tagged with
    another show's Extras are listed there too, as on the site."""
    from jrwi import organize
    db = app.state.db
    upp = "unwitness-protection-program"
    db.upsert("tags", "id", {"id": "9301", "name": "Unwitness Protection Program", "count": 1})
    db.upsert("tags", "id", {"id": "9302", "name": "Prime Defenders Extras", "count": 1})
    db.upsert("tags", "id", {"id": "9303", "name": "UPP Rolled", "count": 1})
    rows = []
    for i, (day, title) in enumerate([
            ("2025-09-13", "Unwitness Protection Program - Diseased Residents [Case File 1]"),
            ("2025-09-15", "Unwitness Protection Program - Diseased Residents [Case File 1] AUDIO ONLY"),
            ("2025-10-04", "Unwitness Protection Program - Diseased Residents [Case File 2]"),
            ("2025-11-08", "Unwitness Protection Program - Diseased Residents [Case File 3]"),
            ("2026-01-02", "Diseased Residents | Just Roll With It: Unwitness Protection Program #1"),   # a re-post
            ("2026-04-05", "Unwitness Protection Program - Empty Graves [Case File 1]"),
            ("2026-05-02", "Unwitness Protection Program - Empty Graves [Case File 2]"),
            ("2026-08-02", "Just Rolled With It | Empty Graves [CASE FILE 1 & 2]"),
            ("2025-10-05", "Just Rolled With It | UPP #2")]):
        pid = 9401 + i
        audio = "AUDIO" in title
        db.upsert("posts", "id", {"id": pid, "url": f"https://example.invalid/{pid}", "title": title,
                                  "path_kind": "audio" if audio else "videos", "status": "complete",
                                  "tag_ids": {9401: ["9301", "9302"], 9408: ["9303"], 9409: ["9303"]}.get(pid, ["9301"]),
                                  "published": day + "T00:00:00+00:00"})
        rows.append(pid)
    try:
        organize.classify_all(db)
        got = {p["id"]: p for p in db.q("SELECT * FROM posts WHERE id >= 9401 AND id < 9500")}
        assert [(got[i]["season"], got[i]["episode"]) for i in (9401, 9403, 9404, 9406, 9407)] == \
            [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)]
        assert (got[9405]["season"], got[9405]["pair_id"]) == (1, 9401)   # the re-post is a version of case file 1
        assert got[9402]["pair_id"] == 9401
        assert got[9408]["tab"] == "aftershow" and got[9408]["episode"] is None
        # Just Rolled With It posts get the season of the episodes they're about: by its name, or by the number
        assert (got[9408]["season"], got[9409]["season"]) == (2, 1)
        assert got[9401]["places"] == [f"{upp}:episodes", "prime-defenders:extras"]
        show = client.get(f"/api/shows/{upp}").json()
        assert [(s["season"], s["name"], s["count"]) for s in show["seasons"]] == \
            [(1, "Diseased Residents", 3), (2, "Empty Graves", 2)]
        rolled = next(t for t in show["tabs"] if t["id"] == "aftershow")
        assert [(s["season"], s["count"]) for s in rolled["seasons"]] == [(1, 1), (2, 1)]
        assert [i["id"] for i in client.get(f"/api/shows/{upp}/items?tab=aftershow&season=1").json()["items"]] == [9409]
        s2 = client.get(f"/api/shows/{upp}/items?tab=episodes&season=2").json()["items"]
        assert [i["id"] for i in s2] == [9407, 9406] and s2[0]["season"] == 2
        pd = {i["id"] for i in client.get("/api/shows/prime-defenders/items?tab=extras&page_size=200").json()["items"]}
        assert 9401 in pd   # listed under the other show's Extras too
        assert {"show": "prime-defenders", "tab": "extras"} in client.get("/api/items/9401").json()["places"]
        web = TestClient(app)
        web.post("/login", data={"password": "test-password"})
        page = web.get(f"/shows/{upp}").text
        assert "Season 1: Diseased Residents" in page and "Season 2: Empty Graves" in page
        assert "Just Rolled With It: Season 1" in page and "Just Rolled With It: Season 2" in page
        assert f"/shows/{upp}?tab=episodes&amp;season=2" in page or f"/shows/{upp}?tab=episodes&season=2" in page
        assert "Empty Graves [Case File 2]" in web.get(f"/shows/{upp}?tab=episodes&season=2").text
        assert "Empty Graves [Case File 2]" not in web.get(f"/shows/{upp}?tab=episodes&season=1").text
        # Jellyfin: the second season's own folder, named
        target = organize.jellyfin_target(db, got[9406])
        assert target[1:] == (2, 1) and "/Season 02/" in target[0]
    finally:
        db.x("DELETE FROM posts WHERE id >= 9401 AND id < 9500")
        db.x("DELETE FROM tags WHERE id IN ('9301', '9302', '9303')")
        organize.classify_all(db)


def test_same_number_twice_keeps_upload_order(app, client):
    """Riptide has two episodes numbered 68 (on purpose, to skip 69): both stay, in upload order. A post without a
    number (a Q&A) goes by when it came out, after the episode that came out before it, in either order."""
    from jrwi import organize
    db = app.state.db
    try:
        for pid, day, title in [(9501, "2022-01-07", "The Greatest Form of Flattery | Riptide #68"),
                                (9502, "2022-01-14", "A Stranger at Sea | Riptide #68"),
                                (9503, "2022-01-21", "Next One | Riptide #70"),
                                # No number: goes by when it came out (its audio, 28 Jan), so after #70. Its video,
                                # the main post, went up after #71's, which mustn't move it past #71.
                                (9504, "2022-01-28", "Riptide Q&A"),
                                (9507, "2022-02-15", "Riptide Q&A (VIDEO)"),
                                (9505, "2022-02-01", "Next Next | Riptide #71"),
                                (9506, "2022-02-10", "Next Next | Riptide #71 (VIDEO)")]:
            db.upsert("posts", "id", {"id": pid, "url": f"https://example.invalid/{pid}", "title": title,
                                      "path_kind": "videos" if pid < 9504 or "VIDEO" in title else "audio",
                                      "status": "complete", "tag_ids": ["101"], "published": day + "T00:00:00+00:00"})
        organize.classify_all(db)
        assert db.post(9504)["pair_id"] == 9507 and db.post(9505)["pair_id"] == 9506
        ids = [i["id"] for i in client.get("/api/shows/riptide/items?tab=episodes&order=asc&page_size=200").json()["items"]]
        mine = [i for i in ids if i >= 9501]
        assert mine == [9501, 9502, 9503, 9507, 9506]
        assert ids.index(9507) == ids.index(9503) + 1 and ids.index(9507) < ids.index(300)   # before #136
        assert db.post(9501)["pair_id"] is None and db.post(9502)["pair_id"] is None   # two #68s, not two versions
        assert db.post(9507)["episode"] is None and db.post(9507)["episode_sort"] == db.post(9504)["episode_sort"] == 70.5
        newest = [i["id"] for i in client.get("/api/shows/riptide/items?tab=episodes&page_size=200").json()["items"]]
        assert [i for i in newest if i >= 9501] == [9506, 9507, 9503, 9502, 9501]
    finally:
        db.x("DELETE FROM posts WHERE id >= 9501 AND id < 9600")
        organize.classify_all(db)


def test_file_inspector(app, client):
    """Every file of a post through the API, and what's inside PDFs, spreadsheets, documents, text and zips."""
    import io
    import zipfile
    import openpyxl
    db, archiver = app.state.db, app.state.archiver
    got = client.get("/api/posts/299/files").json()
    by_role = {}
    for f in got["files"]:
        by_role.setdefault(f["role"], []).append(f)
    sheet = by_role["attachment"][0]
    assert sheet["name"] == "Zombie Character Sheets.pdf" and sheet["type"] == "pdf" and sheet["mime"] == "application/pdf"
    assert sheet["saved"] and sheet["post_id"] == 299 and client.get(sheet["url"]).status_code == 200
    link = by_role["link"][0]   # Google Docs are off in these tests, so the doc is a link that wasn't saved
    assert link["source"].startswith("https://docs.google.com/document/d/") and not link["saved"] and link["url"] is None
    docs = [f for f in client.get("/api/posts/290/files").json()["files"] if f["role"] == "document"]
    assert {d["name"] for d in docs} >= {"Map.pdf", "Big Map.pdf"}
    assert all(d["type"] == "pdf" and d["source"].startswith("https://drive.google.com/") for d in docs)
    assert by_role["image"][0]["type"] == "image" and by_role["image"][0]["preview"] is None
    # A PDF: its pages, each as a picture
    p = client.get(sheet["preview"]).json()
    assert p["type"] == "pdf" and p["pages"] == 2
    page = client.get(p["page_url"].replace("{n}", "2"))
    assert page.status_code == 200 and page.headers["content-type"] == "image/png" and page.content[:4] == b"\x89PNG"
    assert client.get(p["page_url"].replace("{n}", "9")).status_code == 404
    # Items list every version's files; the audio comes with its address for playing
    files = client.get("/api/items/300/files").json()["files"]
    assert {f["post_id"] for f in files} >= {300, 298}
    audio = next(f for f in files if f["role"] == "audio")
    assert audio["url"] == "/api/audio/298" and audio["type"] == "audio" and audio["mime"].startswith("audio/")
    assert any(f["role"] == "video" and f["url"] == "/api/video/300" for f in files)
    # Other kinds of file, as Drive might hand them over
    folder = archiver.paths.archive / "inspect-test"
    folder.mkdir(exist_ok=True)
    wb = openpyxl.Workbook()
    wb.active.title = "Cast"
    wb.active.append(["Name", "Class", "Level"])
    wb.active.append(["Ferin", "Rogue", 7])
    wb.create_sheet("Loot").append(["Gold", 120.0])
    wb.save(folder / "cast.xlsx")
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = (f'<w:document xmlns:w="{w}"><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Session notes</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>The crew reach the harbour.</w:t></w:r></w:p>'
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr><w:r><w:t>Find the captain</w:t></w:r></w:p>'
            '</w:body></w:document>')
    with zipfile.ZipFile(folder / "notes.docx", "w") as z:
        z.writestr("word/document.xml", body)
    with zipfile.ZipFile(folder / "handouts.zip", "w") as z:
        z.writestr("maps/harbour.png", b"x" * 10)
        z.writestr("readme.txt", "hi")
    (folder / "transcript.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello, everyone!\n")
    (folder / "drive-file.bin").write_bytes((folder / "cast.xlsx").read_bytes())   # a Drive file without a name
    t = client.get("/api/preview/inspect-test/cast.xlsx").json()
    assert t["type"] == "table" and [s["name"] for s in t["sheets"]] == ["Cast", "Loot"]
    assert t["sheets"][0]["rows"] == [["Name", "Class", "Level"], ["Ferin", "Rogue", "7"]]
    assert t["sheets"][1]["rows"] == [["Gold", "120"]]
    assert client.get("/api/preview/inspect-test/drive-file.bin").json()["type"] == "table"
    d = client.get("/api/preview/inspect-test/notes.docx").json()
    assert d["type"] == "document" and d["blocks"] == [
        {"kind": "heading", "level": 1, "text": "Session notes"},
        {"kind": "paragraph", "text": "The crew reach the harbour."}, {"kind": "item", "text": "Find the captain"}]
    z = client.get("/api/preview/inspect-test/handouts.zip").json()
    assert z["type"] == "archive" and [e["name"] for e in z["entries"]] == ["maps/harbour.png", "readme.txt"]
    s = client.get("/api/preview/inspect-test/transcript.srt").json()
    assert s["type"] == "text" and s["format"] == "subtitles" and "Hello, everyone!" in s["text"]
    assert client.get("/api/preview/inspect-test/cast.xlsx?page=1").status_code == 400
    assert client.get("/api/preview/../../etc/passwd").status_code == 404
    assert client.get("/api/preview/inspect-test/nothing.pdf").status_code == 404
    assert TestClient(app).get("/api/posts/299/files").status_code == 401   # needs the token or a login
    # The web pages
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    page = web.get("/items/299/files").text
    assert "Zombie Character Sheets.pdf" in page and "inspector.js" in page and "Linked Google file" in page
    assert '/items/299/files?f=' in web.get("/items/299").text
    home = web.get("/").text   # the feed: Original post in place of Share, and a Files button
    assert "Original post" in home and 'href="/items/299/files"' in home and "data-share" not in home
    assert 'href="/items/296/files"' not in home   # a post above the tier has no files
    summary = web.get("/admin/files").text
    assert "PDF" in summary and ".pdf" in summary and "what's inside" in summary



def test_pictures_and_pdfs_in_one_viewer(app):
    """A post's pictures, picture attachments and PDFs (page by page) go in one picture viewer, on the post's page and
    in the feed; other files still open in the inspector."""
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    page = web.get("/items/299").text
    group = re.findall(r'<a [^>]*data-lightbox="g299"[^>]*>', page)
    assert len(group) >= 2 and 'data-lightbox="g299"' in group[0] and "data-pdf" not in group[0]   # the picture first
    pdf = next(a for a in group if "Zombie" in a)
    assert 'data-pdf="/api/preview/posts/299/attachments/Zombie%20Character%20Sheets.pdf"' in pdf
    assert 'data-file="/files/posts/299/attachments/Zombie%20Character%20Sheets.pdf"' in pdf
    assert 'href="/items/299/files?f=' in pdf   # without the page's script, the inspector as before
    feed = web.get("/").text
    assert re.search(r'<a [^>]*data-lightbox="p299"[^>]*data-pdf=', feed)
    # The viewer reads the PDF's pages from the preview
    preview = web.get("/api/preview/posts/299/attachments/Zombie%20Character%20Sheets.pdf").json()
    assert preview["pages"] >= 1 and web.get(preview["page_url"].replace("{n}", "1")).headers["content-type"] == "image/png"

def test_continue_watching_on_home(app, client):
    """Home lists started, unfinished episodes under the show tiles, most recently played first, each opening the
    version that was played."""
    import re
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    client.put("/api/progress/297", json={"position": 3.0, "duration": 20.0, "finished": False})   # barely started
    client.put("/api/progress/300", json={"position": 19.5, "duration": 20.0, "finished": True})   # finished
    client.put("/api/progress/287", json={"position": 12.0, "duration": 20.0, "finished": False})
    time.sleep(1.1)
    client.put("/api/progress/288", json={"position": 15.0, "duration": 20.0, "finished": False})  # audio, played last
    home = web.get("/").text
    assert "Continue watching" in home
    section = home.split('class="resume"', 1)[1].split("</section>", 1)[0]
    links = re.findall(r'class="resume-card" href="(/items/\d+)"', section)
    assert links.index("/items/288") < links.index("/items/287")
    assert "/items/297" not in links and "/items/300" not in links
    assert "0:05 left" in section and "Audio" in section
    assert "Continue watching" not in web.get("/?page=2").text and "Continue watching" not in web.get("/?q=Riptide").text


def test_check_status_says_what_it_does(app, client):
    """Each step of a check has a stable name and plain wording, and says what it's counting."""
    from jrwi.crawler import STEPS
    archiver = app.state.archiver
    seen = []
    original = archiver.status.set

    def recording(**kw):
        original(**kw)
        seen.append(archiver.status.snapshot())

    archiver.status.set = recording
    try:
        assert archiver.run_check(full=True)["result"] == "ok"
    finally:
        archiver.status.set = original
    steps = [x["step"] for x in seen]
    for step in ("starting", "feed", "pages", "posts", "finishing"):
        assert step in steps, step
    assert steps.index("feed") < steps.index("posts") < steps.index("finishing")
    phases = {x["step"]: x["phase"] for x in seen}
    assert phases["starting"] == "Starting a full check" and phases["feed"] == "Reading the whole members feed"
    assert phases["finishing"] == STEPS["finishing"][0]
    assert any(x["step"] == "feed" and x["detail"].startswith("Page 1: ") and " posts read, " in x["detail"] for x in seen)
    assert any(x["step"] == "pages" and x["unit"] == "page" for x in seen)
    assert seen[-1]["step"] == "idle" and not seen[-1]["running"]
    st = client.get("/api/status").json()
    assert st["step"] == "idle" and "unit" in st["progress"]
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    page = web.get("/admin").text
    assert "What do these do?" in page and "back to the very first post" in page
    # Each button: an icon, its name and what it does; the next check comes from the status, kept current by the page
    for name, what in (("Check now", "Look for new posts and save them"),
                       ("Full check", "Read the whole feed again, back to the first post"),
                       ("Re-read saved pages", "without downloading"), ("Settings", "Login, schedule"),
                       ("Shows", "their pictures and tags"), ("Files", "types of file")):
        assert re.search(r'class="action[^"]*"[^>]*>\s*<svg[^>]*>.*?</svg><b>' + re.escape(name) + r'</b>\s*<span>[^<]*'
                         + re.escape(what), page, re.S), name
    assert "data-schedule" in page and "next check (UTC)" not in page


def test_feed_by_tag(app):
    """Home can show one tag's posts, a search keeps to the tag picked, and searching finds tag names too."""
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    art = web.get("/?tag=Art").text
    assert 'href="/items/290"' in art and 'href="/items/300"' not in art
    assert "1 post tagged <b>Art</b>" in art and '<option value="Art" selected>' in art
    assert "Continue watching" not in art
    assert 'href="/items/290"' in web.get("/?tag=art").text            # any capitals
    both = web.get("/?tag=RIPTIDE&q=Blue").text                          # a search keeps to the tag picked
    assert 'href="/items/289"' in both and 'href="/items/290"' not in both and 'href="/items/297"' not in both
    none = web.get("/?tag=Art&q=Blue").text
    assert "0 posts tagged <b>Art</b> and with \"Blue\"" in none.replace("&#34;", '"') and 'href="/items/289"' not in none
    assert 'href="/?q=Blue&amp;tag=RIPTIDE' in both                      # sort and page links keep both
    named = web.get("/?q=Riptide Rolled").text                           # the name of a tag a post carries
    assert 'href="/items/297"' in named
    assert "0 posts tagged <b>Nonsense</b>" in web.get("/?tag=Nonsense").text
    home = web.get("/").text
    assert 'href="/?tag=Art"' in home and '<option value="Riptide Rolled">Riptide Rolled (' in home
    assert 'href="/?tag=RIPTIDE"' in web.get("/items/300").text           # the post page's tags too


def test_api_for_the_apps(app, client):
    """Contract v0.17: tags, the feed, Continue watching, Stop, the archive's stats, and the ping saying it has them."""
    from jrwi import CONTRACT, FEATURES
    ping = TestClient(app).get("/api/ping").json()   # needs no token
    assert ping["contract"] == CONTRACT == "0.27" and set(ping["features"]) == set(FEATURES)
    assert {"post_progress", "tags", "continue", "stop", "feed", "stats"} <= set(ping["features"])

    # Tags: listed with counts, picked by name whatever the capitals, and found by q
    tags = client.get("/api/tags").json()
    assert tags == sorted(tags, key=lambda t: t["name"].lower()) and set(tags[0]) == {"name", "count"}
    art = next(t for t in tags if t["name"] == "Art")
    posts = client.get("/api/posts?tag=art").json()
    assert [p["id"] for p in posts["items"]] == [290] and posts["total"] == art["count"] == 1
    assert client.get("/api/posts?tag=Nonsense").json()["total"] == 0
    both = [p["id"] for p in client.get("/api/posts?tag=RIPTIDE&q=Blue").json()["items"]]
    assert 289 in both and 290 not in both and 297 not in both
    assert 297 in [p["id"] for p in client.get("/api/posts?q=Riptide Rolled").json()["items"]]
    items = [i["id"] for i in client.get("/api/items?tag=RIPTIDE").json()["items"]]
    assert 300 in items and 298 not in items   # items by their main post's tags
    assert client.get("/api/items?tag=Nonsense").json()["total"] == 0
    assert 297 in [i["id"] for i in client.get("/api/items?q=Riptide Rolled").json()["items"]]

    # The feed: whole posts with their first two comments and their progress, in the web feed's order
    feed = client.get("/api/feed?page_size=50").json()
    ids = [p["id"] for p in feed["items"]]
    assert ids == [p["id"] for p in client.get("/api/posts?page_size=50").json()["items"]]
    assert feed["page_size"] == 50 and client.get("/api/feed?page_size=500").json()["page_size"] == 50
    by_id = {p["id"]: p for p in feed["items"]}
    p299 = by_id[299]
    assert p299["body_text"].startswith("Hi, scouts!") and p299["attachments"]
    assert len(p299["top_comments"]) == 2 and p299["more_comments"] is True
    assert p299["top_comments"][0]["author"]["name"] == "Sam" and p299["progress"] is None
    assert by_id[300]["top_comments"][0]["pinned"] and by_id[300]["progress"]["post_id"] == 300
    assert [p["id"] for p in client.get("/api/feed?tag=Art").json()["items"]] == [290]
    assert client.get("/api/feed?order=asc&page_size=50").json()["items"][0]["id"] == ids[-1]

    # Continue watching: the web app's row, each with its progress in the version played last
    client.put("/api/progress/287", json={"position": 12.0, "duration": 20.0, "finished": False})
    time.sleep(1.1)
    client.put("/api/progress/288", json={"position": 15.0, "duration": 20.0, "finished": False})
    rows = client.get("/api/continue").json()["items"]
    web_order = [r["progress"]["post_id"] for r in rows]
    assert web_order.index(288) < web_order.index(287)
    audio = next(r for r in rows if r["progress"]["post_id"] == 288)
    assert audio["progress"]["position"] == 15.0 and audio["item"]["audio"]["post_id"] == 288
    assert "progress" in audio["item"] and audio["item"]["id"] == audio["item"]["progress"]["post_id"]
    assert len(client.get("/api/continue?limit=1").json()["items"]) == 1

    # Stop: 409 when nothing runs, 202 while something does
    assert client.post("/api/stop").status_code == 409
    archiver = app.state.archiver
    archiver.status.set(running=True)
    try:
        r = client.post("/api/stop")
        assert r.status_code == 202 and r.json() == {"stopping": True} and archiver.stop_event.is_set()
    finally:
        archiver.status.set(running=False)
        archiver.stop_event.clear()
    assert TestClient(app).post("/api/stop").status_code == 401

    # Re-read saved pages: started in the scheduler's thread, 409 while a check or a re-read runs
    scheduler = app.state.scheduler
    archiver.status.set(running=True)
    try:
        r = client.post("/api/reparse")
        assert r.status_code == 409 and r.json()["error"] and not scheduler.reparse_requested
    finally:
        archiver.status.set(running=False)
    r = client.post("/api/reparse")
    assert r.status_code == 202 and r.json() == {"started": True} and scheduler.reparse_requested
    scheduler.reparse_requested = False
    scheduler.wake.clear()
    assert TestClient(app).post("/api/reparse").status_code == 401

    # The podcast feed, before it's set up
    assert client.get("/api/podcast").json() == {"set_up": False}
    r = client.post("/api/podcast/read")
    assert r.status_code == 409 and r.json()["error"]
    assert TestClient(app).get("/api/podcast").status_code == 401

    # Stats: the checks, the files by type, and who comments most
    stats = client.get("/api/stats").json()
    runs = app.state.db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 15")
    assert [c["started"] for c in stats["checks"]] == [r["started"] for r in runs]
    assert set(stats["checks"][0]) == {"started", "finished", "result", "message", "new_items", "archived"}
    assert any(f["type"] == "pdf" and f["count"] >= 1 for f in stats["files"])
    total = app.state.db.one("SELECT COUNT(*) AS n FROM comments")["n"]
    c = stats["comments"]
    assert c["total"] == total and 0 < c["replies"] < total
    assert len(c["top"]) <= 10 and c["top"] == sorted(c["top"], key=lambda t: -t["count"])
    sam = next(t for t in c["top"] if t["name"] == "Sam")
    assert sam["avatar"].startswith("/files/avatars/") and sam["count"] >= 1 and sam["likes"] >= 4
    assert set(sam) == {"id", "name", "avatar", "badges", "color", "count", "likes"} and sam["id"] == "81000001"
    # Kim renamed herself Alex: one member, under the newer name; the user's own comments go by name
    names = {r["author_name"] for r in app.state.db.q("SELECT DISTINCT author_name FROM comments")}
    assert {"Kim", "Alex"} <= names and c["commenters"] == len(names) - 1
    kim = next(t for t in c["top"] if t["id"] == "81000004")
    assert kim["name"] == "Alex" and kim["count"] == 2 and kim["likes"] == 327
    lee = next(t for t in c["top"] if t["name"] == "Lee")
    assert lee["id"] is None and lee["count"] == 1


def test_podcast_feed(app, client, monkeypatch):
    """The private podcast feed: kept without its key, matched to posts for the time of day they came out, and a
    new episode in it starts a check of the site, a few times at most."""
    import requests as rq
    from jrwi import podcast
    db, scheduler = app.state.db, app.state.scheduler
    key = "feedkey123secret"
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    assert "Not set up" in web.get("/admin").text
    s = db.settings()   # the form sends every setting: keep the tests' own
    form = {"podcast_feed": f"http://127.0.0.1:{PORT}/supporters/podcast/{key}/rss", "feed_minutes": "15",
            **{k: str(s[k]) for k in ("check_interval_hours", "max_height", "comment_refresh_days", "request_delay",
                                      "recheck_days", "site_email", "public_url", "ta_url", "notify_url",
                                      "watched_minutes_left", "transcript_threads", "transcript_model",
                                      "transcripts_when", "transcripts_from", "transcripts_until")},
            **{k: "on" for k in ("video", "audio", "images", "attachments", "comments", "google_docs",
                                 "public_pages", "youtube_embeds", "handover", "transcripts_read") if s[k]}}
    web.post("/admin/settings", data=form)
    assert db.settings() == {**s, "podcast_feed": form["podcast_feed"], "feed_state": {}}
    assert db.setting("podcast_feed").endswith(f"/{key}/rss") and scheduler.feed_due()

    started = []
    monkeypatch.setattr(scheduler, "check_now", lambda full=False: started.append(full) or True)
    first = scheduler.read_feed()
    assert first["ok"] and first["changed"] and first["matched"] == 2
    assert not first["start_check"] and not scheduler.feed_due()   # the first read is the baseline
    # Matched by the post's address, and by its title; the bonus episode isn't in the archive
    assert db.post(288)["published_at"] == "2026-10-17T18:04:00+00:00"
    assert db.post(298)["published_at"] == "2026-11-07T17:30:00+00:00"
    item = db.one("SELECT * FROM feed_items WHERE guid = 'b0nu5-tavern'")
    assert item["post_id"] is None and item["duration"] == 3723 and item["enclosure_host"] == "cdn.example.com"
    assert "itunes:duration" in item["feed_fields"] and "enclosure" in item["feed_fields"]
    # The API: posts and their items, and each version
    assert client.get("/api/posts/288").json()["published_at"] == "2026-10-17T18:04:00+00:00"
    full = client.get("/api/items/300").json()
    assert full["published_at"] == "2026-11-07T17:30:00+00:00"   # from its audio version
    assert {v["post_id"]: v["published_at"] for v in full["versions"]}[298] == "2026-11-07T17:30:00+00:00"
    assert client.get("/api/posts/299").json()["published_at"] is None
    # The key is never kept outside the setting, nor shown
    for r in db.q("SELECT * FROM feed_items"):
        assert key not in json.dumps(r)
    assert key not in json.dumps(db.setting("feed_state"))
    admin = web.get("/admin").text
    assert key not in admin and key not in web.get("/admin/settings").text
    assert "2 of 3 episodes are posts in the archive" in admin and "Bonus: Tavern Talk" in admin
    assert "<code>itunes:duration</code>" in admin and "2 say the time of day" not in admin
    assert "3 say the time of day they came out, which 2 posts now show" in admin
    assert 'data-time-of-day="2026-10-17T18:04:00+00:00"' in web.get("/items/288").text
    # The same in the API, without the address
    api = client.get("/api/podcast")
    assert key not in api.text and "127.0.0.1" not in api.text
    p = api.json()
    assert p["set_up"] and p["ok"] and p["error"] is None and p["last_read"] and p["title"]
    assert (p["count"], p["matched"], p["with_time"], p["every_minutes"]) == (3, 2, 3, 15)
    assert p["last_trigger"] is None and p["times_used"] == 2
    assert [e["post_id"] for e in p["latest"]] == [r["post_id"] for r in podcast.summary(db)["latest"]]
    assert set(p["latest"][0]) == {"title", "published", "post_id", "duration"}
    bonus = next(e for e in p["latest"] if e["title"] == "Bonus: Tavern Talk")
    assert bonus["post_id"] is None and bonus["duration"] == 3723
    assert [u["title"] for u in p["unmatched"]] == ["Bonus: Tavern Talk"]
    assert any(f["name"] == "itunes:duration" and f["count"] >= 1 for f in p["fields"])

    # Nothing new: the feed answers 304 and nothing starts
    again = scheduler.read_feed()
    assert again["ok"] and not again["changed"] and not again["start_check"]
    # A new episode the site hasn't shown yet: a check starts, and again at most every 30 minutes, 3 times
    rq.get(f"http://127.0.0.1:{PORT}/_test/feed?n=1", timeout=5)
    try:
        r = scheduler.read_feed()
        assert r["start_check"] and r["new"] == ["Something New | Riptide #137 (AUDIO ONLY)"]
        assert not scheduler.read_feed(force=True)["start_check"]   # tried just now
        db.x("UPDATE feed_items SET last_try = '2000-01-01T00:00:00+00:00' WHERE guid = 'fresh-episode-1'")
        assert scheduler.read_feed(force=True)["start_check"]
        web.post("/admin/feed/read")                                  # "Read it now" starts the check itself
        db.x("UPDATE feed_items SET last_try = '2000-01-01T00:00:00+00:00' WHERE guid = 'fresh-episode-1'")
        r = client.post("/api/podcast/read")                         # the API's Read it now, in the background
        assert r.status_code == 202 and r.json() == {"reading": True}
        for t in threading.enumerate():
            if t.name == "read-feed":
                t.join(10)
        assert started == [False]
        assert db.one("SELECT tries FROM feed_items WHERE guid = 'fresh-episode-1'")["tries"] == 3
        db.x("UPDATE feed_items SET last_try = '2000-01-01T00:00:00+00:00' WHERE guid = 'fresh-episode-1'")
        assert not scheduler.read_feed(force=True)["start_check"]   # three tries are enough
        assert "Last started a check" in web.get("/admin").text
        trigger = client.get("/api/podcast").json()["last_trigger"]
        assert trigger["at"] and trigger["titles"] == ["Something New | Riptide #137 (AUDIO ONLY)"]
    finally:
        rq.get(f"http://127.0.0.1:{PORT}/_test/feed?n=0", timeout=5)
    # A wrong address: the error names the host, never the key
    db.set_setting("podcast_feed", f"http://127.0.0.1:{PORT}/supporters/podcast/wrong{key}/rss")
    bad = podcast.poll(db, force=True)
    assert not bad["ok"] and "HTTP 404" in bad["error"] and key not in bad["error"]
    db.set_setting("podcast_feed", "")
    db.set_setting("feed_state", {})


def test_feed_is_read_back_far_enough():
    """An incremental check reads on past its known posts until it's back past the days it always rereads."""
    from jrwi.crawler import STOP_AFTER_KNOWN, feed_read_far_enough
    assert not feed_read_far_enough(STOP_AFTER_KNOWN - 1, "2020-01-01", "2026-09-22")
    assert not feed_read_far_enough(STOP_AFTER_KNOWN, "2026-10-01", "2026-09-22")   # still inside the window
    assert feed_read_far_enough(STOP_AFTER_KNOWN, "2026-09-20", "2026-09-22")
    assert feed_read_far_enough(STOP_AFTER_KNOWN, "2026-10-01", None)                # window turned off
    assert feed_read_far_enough(STOP_AFTER_KNOWN, None, "2026-09-22")                # cards without dates


def test_video_handed_over_to_tubearchivist(app, client):
    """Once an episode's YouTube version is up and TubeArchivist has it, the archived video is deleted and the item
    plays from YouTube. Positions move with the measured gap; a video whose length differs is kept. Runs last: it
    changes the mock site."""
    import requests as rq
    from jrwi import handover
    db, archiver = app.state.db, app.state.archiver
    web = TestClient(app)
    web.post("/login", data={"password": "test-password"})
    old_file = Path(os.environ["JRWI_MEDIA"]) / db.post(297)["media"]["video"]["path"]
    keeper_file = Path(os.environ["JRWI_MEDIA"]) / db.post(287)["media"]["video"]["path"]
    assert old_file.is_file() and keeper_file.is_file()
    client.put("/api/progress/297", json={"position": 5.0, "duration": 20.0, "finished": False})
    # The site adds the YouTube posts; TubeArchivist isn't connected yet, so nothing is deleted
    rq.get(f"http://127.0.0.1:{PORT}/_test/stage?n=1", timeout=5)
    assert archiver.run_check()["result"] == "ok"
    assert db.post(295)["pair_id"] == 297 and db.post(294)["pair_id"] == 287
    assert db.post(297)["handover"]["status"] == "waiting" and old_file.is_file()
    assert db.post(297)["site_gone"] and not db.post(295)["site_gone"]   # the members-only post is gone from the site
    assert "connect TubeArchivist" not in web.get("/admin").text and "isn't connected" in web.get("/admin").text
    # Connect TubeArchivist on the Settings page; the token is never shown again
    web.post("/admin/settings", data={"ta_url": f"http://127.0.0.1:{PORT}/ta", "ta_token": "wrong", "handover": "on",
                                      "recheck_days": "60", "video": "on", "audio": "on", "images": "on",
                                      "attachments": "on", "comments": "on", "public_pages": "on"})
    assert "turned the API token down" in web.post("/admin/test-ta").text
    # Connected, with deleting turned off: the archived video is kept, and the gap to TubeArchivist's copy (3 s) is
    # measured once, so the YouTube post's transcript comes in its own timeline (0.14.8, the Tubedroid chat's
    # suggestion 13); the archived post's stays in the archived video's
    import jrwi.transcripts as tr
    web.post("/admin/settings", data={"ta_url": f"http://127.0.0.1:{PORT}/ta", "ta_token": "ta-test-token",
                                      "recheck_days": "60", "video": "on", "audio": "on", "images": "on",
                                      "attachments": "on", "comments": "on", "public_pages": "on"})
    assert archiver.run_check()["result"] == "ok"
    h = db.post(297)["handover"]
    assert h["status"] == "ready" and old_file.is_file() and h["yt_shift_checked"], h
    assert abs(h["yt_shift"] - 3.0) < 0.1 and h["yt_shift_for"]["youtube_id"] == "JRWI136ytAA"
    measured_at = h["yt_shift_at"]
    tx = app.state.scheduler.transcripts
    tx.dir.mkdir(parents=True, exist_ok=True)
    tx._write(tx.lines_file(297), {"version": tr.VERSION, "document": {"path": "x.pdf", "size": 1}, "aligned": True,
                                   "timeline": "video", "lines": [{"speaker": "Grizzly", "text": "Ahoy.",
                                                                   "start": 10.0, "end": 12.0}]})
    db.upsert("transcripts", "item_id", {
        "item_id": 297, "document": "x.pdf", "doc_size": 1, "lines": 1, "aligned": 1, "updated_at": "x",
        "timing": {"at": "2026-11-23T00:00:00+00:00", "found": 0.9, "sound_post": 297, "timeline": "video",
                   "model": "large-v3-turbo"}})
    yt = client.get("/api/transcript/295").json()
    assert yt["timeline"] == "youtube" and abs(yt["lines"][0]["start"] - (10.0 + h["yt_shift"])) < 0.01, yt
    own = client.get("/api/transcript/297").json()
    assert own["timeline"] == "video" and own["lines"][0]["start"] == 10.0
    assert archiver.run_check()["result"] == "ok"            # not measured again for the same copy
    assert db.post(297)["handover"]["yt_shift_at"] == measured_at
    assert "its transcript is moved to match" in web.get("/admin/items/297").text
    web.post("/admin/settings", data={"ta_url": f"http://127.0.0.1:{PORT}/ta", "ta_token": "ta-test-token",
                                      "handover": "on", "recheck_days": "60", "video": "on", "audio": "on",
                                      "images": "on", "attachments": "on", "comments": "on", "public_pages": "on"})
    page = web.post("/admin/test-ta").text
    assert "connected, TubeArchivist v0.5.4" in page and "ta-test-token" not in page
    assert archiver.run_check()["result"] == "ok"
    # Same length within 10 s: handed over, the file and its Jellyfin files are gone
    h = db.post(297)["handover"]
    assert h["status"] == "done" and h["youtube_id"] == "JRWI136ytAA" and h["shift_checked"]
    assert abs(h["shift"] - 3.0) < 0.1 and h["freed"] > 0
    assert not old_file.exists() and not list(old_file.parent.glob(old_file.stem + "*"))
    item = client.get("/api/items/297").json()
    assert item["video"]["youtube_id"] == "JRWI136ytAA" and item["video"]["post_id"] == 295
    assert item["video"]["handed_over"]["post_id"] == 297 and item["kind"] == "youtube"
    post = client.get("/api/posts/297").json()
    assert post["video"] is None and post["handed_over"]["post_id"] == 295 and post["removed_from_site"]
    # Progress saved in the archived video's timeline moved 3 s, and the old post ID still works
    at = client.get("/api/progress/297").json()
    assert abs(at["position"] - 8.0) < 0.15, at
    assert client.get("/api/video/297").status_code == 404
    assert any(e["type"] == "video_handed_over" and e["post_id"] == 297 for e in client.get("/api/events").json())
    assert "watch the YouTube version" in web.get("/items/297").text
    # The transcript heard from the deleted video is moved by the gap measured at the hand-over, until the episode is
    # heard again from the YouTube video
    after = client.get("/api/transcript/295").json()
    assert after["timeline"] == "video" and abs(after["lines"][0]["start"] - (10.0 + h["shift"])) < 0.01, after
    tx.lines_file(297).unlink()
    db.x("DELETE FROM transcripts WHERE item_id=297")
    assert "TubeArchivist has the same video from YouTube" in web.get("/admin/items/295").text
    # A different length: both are kept until the user says otherwise
    h = db.post(287)["handover"]
    assert h["status"] == "differs" and keeper_file.is_file()
    page = web.get("/admin/items/287").text
    assert "Delete it anyway" in page and "Keep the archived video" in page
    # YouTube-only episodes with audio are lined up against TubeArchivist's copy (3 s intro, audio 2 s in)
    sync = client.get("/api/items/289").json()["sync"]
    assert sync["checked"] and abs(sync["offset"] - (-5.0)) < 0.1, sync
    # Another check doesn't download the deleted video again
    assert archiver.run_check()["result"] == "ok"
    assert not old_file.exists() and db.post(297)["handover"]["status"] == "done"
    # Deleting anyway, then keeping
    web.post("/admin/items/287/handover", data={"choice": "force"})
    handover.run(db, archiver.paths, db.settings())
    assert db.post(287)["handover"]["status"] == "done" and not keeper_file.exists()
    assert "already deleted" in web.post("/admin/items/287/handover", data={"choice": "force"}).text


def test_security(app, client):
    """Headers on pages and files, forms from other sites refused, a wait after wrong passwords, logins that only
    send you to this site, and saved HTML that can't run anything."""
    from jrwi.web.guard import safe_link, safe_next
    from jrwi.web.serialize import clean_html
    paths = app.state.archiver.paths
    web = TestClient(app, client=("10.9.8.7", 50000))
    web.post("/login", data={"password": "test-password"})
    page = web.get("/admin")
    csp = page.headers["content-security-policy"]
    assert page.status_code == 200 and "script-src 'self'" in csp and "frame-ancestors 'self'" in csp
    assert page.headers["x-frame-options"] == "SAMEORIGIN" and page.headers["x-content-type-options"] == "nosniff"
    assert 'data-confirm="Fetch every archived post' in page.text and "onsubmit" not in page.text
    assert "server" not in {k.lower() for k in page.headers} or "uvicorn" not in page.headers.get("server", "")
    # A file a browser would run as a page opens sandboxed; a PDF opens as itself
    odd = paths.archive / "posts" / "299" / "attachments" / "notes.html"
    odd.parent.mkdir(parents=True, exist_ok=True)
    odd.write_text("<script>alert(1)</script>")
    r = client.get("/files/posts/299/attachments/notes.html")
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("sandbox")
    pdf = next(f for f in client.get("/api/posts/299/files").json()["files"] if f["type"] == "pdf")
    r = client.get(pdf["url"])
    assert r.status_code == 200 and "content-security-policy" not in r.headers and r.headers["x-content-type-options"] == "nosniff"
    odd.unlink()
    # Changes from another site's page are refused; from the archive's own, and from apps, they go through
    assert web.post("/admin/stop", headers={"Origin": "http://evil.example"}).status_code == 403
    assert web.post("/admin/stop", headers={"Origin": "http://box:8096"}).status_code == 403   # same server, other port
    assert web.post("/admin/stop", headers={"Referer": "http://evil.example/x"}).status_code == 403
    assert web.post("/admin/stop", headers={"Origin": "null"}).status_code == 403
    assert web.post("/admin/stop", headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 303
    assert web.post("/admin/stop", follow_redirects=False).status_code == 303
    assert client.post("/api/stop").status_code == 409   # the app's token, no Origin: as before
    # Logging in sends you only to this site
    for bad in ("//evil.example", "/\\evil.example", "https://evil.example", "/\tx"):
        assert safe_next(bad) == "/"
    assert safe_next("/admin?x=1") == "/admin?x=1"
    r = web.post("/login", data={"password": "test-password", "next": "/\\evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert safe_link("javascript:alert(1)") == "#" and safe_link("java\tscript:x") == "#"
    assert safe_link("https://jrwishow.com/x") == "https://jrwishow.com/x" and safe_link("/items/1") == "/items/1"
    # Five wrong passwords, then that address waits, even with the right one; others don't
    guesser = TestClient(app, client=("10.1.2.3", 50001))
    for _ in range(5):
        assert guesser.post("/login", data={"password": "wrong"}).status_code == 401
    r = guesser.post("/login", data={"password": "test-password"})
    assert r.status_code == 429 and "Try again in" in r.text
    assert TestClient(app, client=("10.1.2.4", 50002)).post(
        "/login", data={"password": "test-password"}, follow_redirects=False).status_code == 303
    # Saved HTML keeps its formatting and loses anything that could run
    for bad in ('<a href="java\tscript:alert(1)">x</a>', '<a href="\x01javascript:alert(1)">x</a>',
                '<p>a<!-- --!><img src=x onerror=alert(1)> --></p>', '<img srcset="/a.png 1x, javascript:x 2x">',
                '<svg onload=alert(1)></svg>', '<p onclick="alert(1)">p</p>', '<iframe src="https://evil.example"></iframe>'):
        out = clean_html(bad)
        assert "javascript" not in out and "onerror" not in out and "onload" not in out and "onclick" not in out
        assert "evil" not in out and "<!--" not in out
    assert clean_html('<p>Hi <b>there</b> <a href="https://x.example/a">link</a></p>').startswith('<p>Hi <b>there</b> <a href="https://x.example/a"')
    assert 'src="data:image/png;base64,AAAA"' in clean_html('<img src="data:image/png;base64,AAAA">')
    # A wrong token with odd characters is just wrong
    assert TestClient(app).get("/api/status", params={"token": "ünïcode"}).status_code == 401


def test_transcripts_are_read_timed_and_served(app, client, monkeypatch, tmp_path):
    from transcript_pdf import write
    db, scheduler, paths = app.state.db, app.state.scheduler, app.state.archiver.paths
    name = "Episode transcription here. [1RkmhusZ].pdf"
    write(paths.archive / "google-docs" / name, [
        ("Zach", "There is something wrong with this town"), ("Charlie", "What?"), (None, "[laughter]"),
        ("Zach", "And no one seems to notice at all"), ("Nate", "Okay then let us go")])
    before = db.post(298)["documents"]
    db.update("posts", "id", 298, {"documents": list(before or []) + [
        {"name": name, "url": "google-docs/" + name, "source": "https://docs.google.com/document/d/1RkmhusZ/edit"}]})
    # What the speech recogniser hears in the 20-second test file, in its place for the test
    heard = [[1.0, 1.3, "There"], [1.3, 1.5, "is"], [1.5, 2.0, "something"], [2.0, 2.3, "wrong"], [2.3, 2.5, "with"],
             [2.5, 2.8, "this"], [2.8, 3.2, "town."], [4.0, 4.4, "What?"], [8.0, 8.3, "And"], [8.3, 8.5, "no"],
             [8.5, 8.8, "one"], [8.8, 9.2, "seems"], [9.2, 9.4, "to"], [9.4, 9.9, "notice"], [9.9, 10.1, "at"],
             [10.1, 10.4, "all."], [12.0, 12.3, "Okay,"], [12.3, 12.6, "then"], [12.6, 12.8, "let's"], [12.8, 13.0, "us"],
             [13.0, 13.3, "go."]]
    (tmp_path / "heard.json").write_text(json.dumps(heard))
    monkeypatch.setenv("JRWI_FAKE_HEARING", str(tmp_path / "heard.json"))
    try:
        # A check running alongside can download the file again, move it or hand it over while it's heard:
        # nothing is recorded against the episode, and it's heard again next time
        import jrwi.transcripts as tr
        monkeypatch.setattr(tr, "_same_file", lambda sound: False)
        scheduler._transcripts(one=300)
        row = db.one("SELECT * FROM transcripts WHERE item_id=300")
        assert row["lines"] == 5 and not row["aligned"] and not (row["timing"] or {}).get("at"), row
        monkeypatch.undo()
        monkeypatch.setenv("JRWI_FAKE_HEARING", str(tmp_path / "heard.json"))
        scheduler._transcripts(one=300)   # as the admin page's "Time it now" does
        row = db.one("SELECT * FROM transcripts WHERE item_id=300")
        assert row["aligned"] and row["lines"] == 5 and row["timing"]["placed"] == 5, row
        assert row["timing"]["took"] is not None and row["timing"]["error"] is None
        assert client.get("/api/items/300").json()["transcript"] is True
        assert client.get("/api/posts/298").json()["transcript"] is True
        assert "transcript" in client.get("/api/ping").json()["features"]
        audio = client.get("/api/transcript/298").json()
        assert audio["aligned"] and audio["document"].startswith("/files/google-docs/")
        assert audio["lines"][0] == {"start": 1.0, "end": 3.2, "speaker": "Zach",
                                     "text": "There is something wrong with this town"}
        assert audio["lines"][3]["start"] == 8.0 and audio["lines"][4]["start"] == 12.0
        note = audio["lines"][2]
        assert note["speaker"] is None and audio["lines"][1]["start"] <= note["start"] <= 8.0
        # The video's lines are in the video's timeline: audio = video + offset
        offset = client.get("/api/items/300").json()["sync"]["offset"]
        video = client.get("/api/transcript/300").json()
        assert abs(video["lines"][3]["start"] - max(0.0, 8.0 - offset)) < 0.01
        # Without a transcript: 404
        other = next(r["id"] for r in db.q("SELECT id FROM posts WHERE id NOT IN (298, 300) LIMIT 1"))
        assert client.get(f"/api/transcript/{other}").status_code == 404
        # The pages show it
        web = TestClient(app)
        web.post("/login", data={"password": "test-password"})
        assert "data-transcript" in web.get("/items/298").text
        admin = web.get("/admin").text
        assert "Time one episode now" in admin and "Time transcripts now" in admin and "lines placed" in admin
        assert "data-bg-box" in admin and 'data-had="0"' in admin and 'href="/admin/transcripts"' in admin
        # The record of every try: the page, each episode's tries, and the download
        tries = db.q("SELECT * FROM transcript_runs WHERE item_id=300")
        assert len(tries) == 1 and tries[0]["timing"]["placed"] == 5 and tries[0]["timing"]["sessions"] == 1
        # (it was heard in the try the changed file cut short, which isn't recorded, so this one only matched)
        assert tries[0]["timing"]["heard_now"] is False and tries[0]["timing"]["threads_used"]
        assert tries[0]["timing"]["words"] > 0 and tries[0]["timing"]["match_took"] is not None
        scheduler._transcripts(one=300)   # Time it again, with nothing changed: only matched again
        assert len(db.q("SELECT * FROM transcript_runs WHERE item_id=300")) == 2
        records = web.get("/admin/transcripts?sort=found").text
        assert "Through the Fire | Riptide #136" in records and "Timed" in records and "5 of 5" in records
        assert "2 tries" in records and "1 of" in records   # 1 of N episodes timed
        assert "Post 300 · " in records   # to name it to the PC program, to try it again
        # Lines placed in colour next to the episode: all 5 of 5 is the archive's yellow, with dark text
        assert '<span class="rating" style="background:#f4e434;color:#1b0833"' in records and ">100%</span>" in records
        assert 'class="rating"' in web.get("/admin/items/300").text
        # An episode whose transcript is read but not timed is listed too, under the timed ones
        db.upsert("transcripts", "item_id", {"item_id": other, "document": "x.pdf", "doc_size": 1, "lines": 9,
                                             "aligned": 0, "timing": {}, "updated_at": "x"})
        records = web.get("/admin/transcripts").text
        assert "No sound to hear" in records or "Not timed yet" in records
        db.x("DELETE FROM transcripts WHERE item_id=?", (other,))
        assert "Every try (2)" in web.get("/admin/items/298").text and "matched again" in web.get("/admin/items/300").text
        csv_text = web.get("/admin/transcripts.csv").text.splitlines()
        assert csv_text[0].startswith("item_id,title,at,result") and len(csv_text) == 3
        assert ",timed," in csv_text[1] and ",False," in csv_text[1]   # the newest first: matched again
        # A new way of reading (0.14.5) reads every transcript again and keeps its timing: the words already heard
        # are matched to the new lines, nothing is heard again, and the try is recorded as such
        monkeypatch.setattr(tr, "VERSION", 99)
        done = scheduler.transcripts.run(scheduler.ser, lambda: False, timing_on=False)
        assert done["read"] >= 1 and done["timed"] == 0
        row = db.one("SELECT * FROM transcripts WHERE item_id=300")
        assert row["aligned"] and row["timing"]["placed"] == 5 and row["timing"]["read_again"] is True, row
        assert row["timing"]["heard_now"] is False and row["timing"]["error"] is None
        assert client.get("/api/transcript/298").json()["lines"][3]["start"] == 8.0
        assert len(db.q("SELECT * FROM transcript_runs WHERE item_id=300")) == 3
        assert "transcript read again" in web.get("/admin/items/300").text
        # Debug data (0.14.6): the transcript, its lines, the words heard and every try, and nothing secret
        import io
        import zipfile
        r = web.get("/admin/items/298/transcript-debug.zip")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        assert {"about.json", "lines.json", "heard.json", "tries.json", "transcript.pdf"} <= set(z.namelist())
        about = json.loads(z.read("about.json"))
        assert about["item_id"] == 300 and about["heard"]["words"] > 0 and about["transcript"]["lines"] == 5
        assert len(json.loads(z.read("tries.json"))) == 3 and len(json.loads(z.read("lines.json"))) == 5
        assert json.loads(z.read("heard.json"))["words"]
        blob = b"".join(z.read(n) for n in z.namelist())
        assert b"test-password" not in blob and db.setting("api_token").encode() not in blob
        assert "Debug data" in web.get("/admin/items/300").text
        assert web.get("/admin/items/999999/transcript-debug.zip").status_code == 404
        # While it runs, the top of the admin page has its own bar: the episode's progress, the batch's count
        scheduler.bg.begin("transcripts", total=3, phase="Timing transcripts", running=True)
        scheduler.bg.set(done=1, current=0.42, detail="Paid In Blood · hearing, 42%, about 16 min left")
        try:
            admin = web.get("/admin").text
            assert "episode 2 of 3" in admin and "width:42.0%" in admin and "about 16 min left" in admin
            assert client.get("/api/status").json()["background"]["progress"]["current"] == 0.42
        finally:
            scheduler.bg.set(running=False, step="idle", phase="idle", detail="", done=0, total=0, current=None)
        assert "only when you start it" in admin
        assert "Time it again" in web.get("/admin/items/298").text
        # A YouTube version's page has the transcript too, following the YouTube player
        ser = scheduler.ser
        yt = next(r for r in db.q("SELECT * FROM posts")
                  if (ser.post_full(r).get("video") or {}).get("youtube_id") and not ser.post_full(r).get("audio"))
        yt_primary = ser.primary_of(yt["id"]) or yt
        if yt_primary["id"] != 300:
            db.upsert("transcripts", "item_id", {"item_id": yt_primary["id"], "document": "x.pdf", "doc_size": 1, "lines": 1,
                                                 "aligned": 0, "timing": {}, "updated_at": "x"})
        page = web.get(f"/items/{yt['id']}").text
        assert "data-transcript-toggle" in page and "enablejsapi=1" in page and f'data-yt-post="{yt["id"]}"' in page
        settings = web.get("/admin/settings").text
        assert 'name="transcript_model"' in settings and 'name="transcripts_when"' in settings
        # The schedule: hours are kept only when they're times of day
        form = {k: (str(v) if not isinstance(v, bool) else "on") for k, v in db.settings().items()
                if isinstance(v, (str, int, float)) and (not isinstance(v, bool) or v)}
        form.pop("site_password", None)
        web.post("/admin/settings", data={**form, "transcripts_when": "hours", "transcripts_from": "23:15",
                                          "transcripts_until": "25:00"})
        assert db.setting("transcripts_when") == "hours" and db.setting("transcripts_from") == "23:15"
        assert db.setting("transcripts_until") == "07:00"
        assert "from 23:15 to 07:00" in web.get("/admin").text
        db.set_setting("transcripts_when", "off")
        # Time transcripts now: the tile becomes Pause until it's done or paused
        web.post("/admin/transcripts/all")
        assert scheduler.transcripts_manual and "Pause transcripts" in web.get("/admin").text
        web.post("/admin/transcripts/pause")
        assert not scheduler.transcripts_manual and "Time transcripts now" in web.get("/admin").text
        scheduler.transcripts_paused = False
        # Timing it again with nothing changed only matches again: nothing is heard twice
        monkeypatch.setenv("JRWI_FAKE_HEARING", str(tmp_path / "missing.json"))
        scheduler._transcripts(one=300)
        assert db.one("SELECT aligned FROM transcripts WHERE item_id=300")["aligned"] == 1
        # Another model has to hear it again; one that can't be loaded stops the job, rather than every episode
        # in line being marked as failed, and what was timed before stays
        monkeypatch.delenv("JRWI_FAKE_HEARING")
        db.set_setting("transcript_model", "base.en")
        scheduler._transcripts(one=300)
        assert "couldn't be loaded" in db.setting("transcripts_problem")
        row = db.one("SELECT * FROM transcripts WHERE item_id=300")
        assert row["aligned"] == 1 and not row["timing"].get("error")
        assert "be loaded" in web.get("/admin").text   # an apostrophe is escaped in HTML
        db.set_setting("transcript_model", "small.en")
        db.set_setting("transcripts_problem", "")
        pc_program_hears_and_sends(app, client, web, tmp_path)
    finally:
        db.set_setting("transcript_model", "small.en")
        db.set_setting("transcripts_problem", "")
        db.update("posts", "id", 298, {"documents": before})
        db.x("DELETE FROM transcripts")


def pc_program_hears_and_sends(app, client, web, tmp_path):
    """Hearing on another computer (contract v0.24): the PC program, run for real against the running archiver,
    with stand-in hearing (small.en mishears every fourth word)."""
    import subprocess
    import sys
    import threading
    import zipfile

    import uvicorn
    db = app.state.db
    program = Path(__file__).resolve().parent.parent / "jrwi" / "pc" / "jrwi_hear.py"
    assert web.get("/admin/transcripts/jrwi_hear.py").content == program.read_bytes()
    # The job: the sound, how to hear it, the names to listen for
    job = client.get("/api/transcripts/jobs/298").json()
    assert job["item_id"] == 300 and job["sound"]["url"] == "/api/audio/298" and job["sound"]["size"] > 0
    assert job["hearing"]["chunk"] == 600 and job["hotwords"] is None and job["timed"]
    assert client.get("/api/transcripts/jobs").json()["jobs"] == []   # it's timed: nothing left to hand out
    every = client.get("/api/transcripts/jobs?all=true").json()   # v0.25: every episode, timed or not
    assert [j["item_id"] for j in every["jobs"]] == [300] and every["episodes"] >= 1
    assert every["jobs"][0]["timed_with"] == job["timed_with"] and job["timed_with"]
    assert job["timed_with"] in job["heard_with"]
    assert "transcript_jobs" in client.get("/api/ping").json()["features"]
    # A result for a sound that isn't this episode's is refused
    bad = client.post("/api/transcripts/jobs/300", json={"sound_size": 1, "results": [{"model": "x", "words": []}]})
    assert bad.status_code == 409 and "changed" in bad.json()["error"]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8772, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    import time as _t
    for _ in range(50):
        if server.started:
            break
        _t.sleep(0.1)
    env = dict(os.environ, JRWI_FAKE_HEARING=str(tmp_path / "heard.json"), JRWI_SERVER="http://127.0.0.1:8772",
               JRWI_TOKEN=db.setting("api_token"), PYTHONIOENCODING="utf-8")
    work = tmp_path / "pc"
    work.mkdir()
    pc = work / "jrwi_hear.py"
    pc.write_bytes(program.read_bytes())   # as downloaded: it keeps its cache next to itself
    runs_before = len(db.q("SELECT * FROM transcript_runs WHERE item_id=300"))
    try:
        out = subprocess.run([sys.executable, str(pc), "compare", "298", "--device", "cpu",
                              "--models", "small.en,medium.en"], env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "medium.en" in out.stdout and "<- in use" in out.stdout and "lines both placed" in out.stdout, out.stdout
        assert "against the timing that was in use" in out.stdout and "In use now: medium.en" in out.stdout, out.stdout
        runs = db.q("SELECT * FROM transcript_runs WHERE item_id=300 ORDER BY id")[runs_before:]
        assert [r["timing"]["model"] for r in runs] == ["small.en", "medium.en"]
        small, medium = runs[0]["timing"], runs[1]["timing"]
        assert small["source"] == medium["source"] == "pc" and medium["found"] > small["found"]
        assert medium["compare"]["model"] == "small.en" and medium["compare"]["both"] >= 1
        assert medium["compare_in_use"]["both"] >= 1
        row = db.one("SELECT * FROM transcripts WHERE item_id=300")
        assert row["aligned"] and row["timing"]["model"] == "medium.en" and row["timing"]["used"]
        assert not list((work / "jrwi_hear_cache").glob("*.m4a"))   # the sound is deleted once sent
        page = web.get("/admin/items/300").text
        assert "Compared with small.en" in page and ">PC<" in page and "in use" in page
        assert ">PC<" in web.get("/admin/transcripts").text
        assert ",PC," in web.get("/admin/transcripts.csv").text
        # Without the network: the export, a result file, and the import on the episode's admin page
        z = web.get("/admin/items/298/transcript-job.zip")
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        (work / "job.zip").write_bytes(z.content)
        with zipfile.ZipFile(work / "job.zip") as zf:
            assert "job.json" in zf.namelist() and any(n.startswith("sound/") for n in zf.namelist())
        out = subprocess.run([sys.executable, str(pc), "file", str(work / "job.zip"), "--models", "small.en"],
                             env=dict(env, JRWI_SERVER="", JRWI_TOKEN=""), capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stdout + out.stderr
        result = work / "job-result.json"
        r = web.post("/admin/items/298/transcript-import", files={"result": ("job-result.json", result.read_bytes())},
                     follow_redirects=False)
        # small.en found fewer of the transcript's words than medium.en, which is in use: medium.en stays (v0.25)
        assert r.status_code == 303 and "imported=medium.en&kept=1" in r.headers["location"], r.headers
        assert db.one("SELECT * FROM transcripts WHERE item_id=300")["timing"]["model"] == "medium.en"
        assert "so it stays in use" in web.get(r.headers["location"]).text
        assert db.q("SELECT * FROM transcript_runs WHERE item_id=300 ORDER BY id DESC")[0]["timing"]["model"] == "small.en"
        r = web.post("/admin/items/298/transcript-import", files={"result": ("x.json", b"not json")},
                     follow_redirects=False)
        assert "tx_error=" in r.headers["location"]
        assert "Not imported" in web.get(r.headers["location"]).text
        # Every episode (v0.25): what each model has heard is skipped, so running it again carries on
        out = subprocess.run([sys.executable, str(pc), "check"], env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0 and "can be heard" in out.stdout, out.stdout + out.stderr
        out = subprocess.run([sys.executable, str(pc), "list", "--all"], env=env, capture_output=True, text=True,
                             timeout=120)
        assert out.returncode == 0 and "medium.en" in out.stdout and "small.en" in out.stdout, out.stdout + out.stderr
        out = subprocess.run([sys.executable, str(pc), "run", "--all", "--device", "cpu"], env=env,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0 and "0 to do, 1 already heard with medium.en" in out.stdout, out.stdout + out.stderr
        out = subprocess.run([sys.executable, str(pc), "run", "--all", "--device", "cpu", "--models", "large-v3"],
                             env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0 and "1 to do" in out.stdout and "In use now: large-v3" in out.stdout, \
            out.stdout + out.stderr
        assert "1 episode(s) heard" in out.stdout and "carries on" in out.stdout
        assert db.one("SELECT * FROM transcripts WHERE item_id=300")["timing"]["model"] == "large-v3"
        assert "large-v3" in client.get("/api/transcripts/jobs/300").json()["heard_with"]
    finally:
        server.should_exit = True


def test_youtube_only_episodes_are_heard_from_tubearchivist(app, client, monkeypatch, tmp_path):
    """An episode that's only on YouTube (or whose video was handed over) and has no audio version is heard from
    TubeArchivist's copy: its sound is copied out once, handed to the PC program, and dropped once it's timed (0.14.2).
    Runs after the hand-over test, which connects TubeArchivist."""
    from transcript_pdf import write
    db, scheduler, paths = app.state.db, app.state.scheduler, app.state.archiver.paths
    tx = app.state.transcripts if hasattr(app.state, "transcripts") else scheduler.transcripts
    primary = db.post(297)
    members = [primary] + db.q("SELECT * FROM posts WHERE pair_id=297")
    assert not any((m["media"] or {}).get("audio") for m in members), "item 297 is meant to have no audio"
    name = "Handed over transcript [2HndOvr].pdf"
    write(paths.archive / "google-docs" / name, [
        ("Zach", "There is something wrong with this town"), ("Charlie", "What?"),
        ("Zach", "And no one seems to notice at all"), ("Nate", "Okay then let us go")])
    db.update("posts", "id", 295, {"documents": list(db.post(295)["documents"] or []) + [
        {"name": name, "url": "google-docs/" + name, "source": "https://docs.google.com/document/d/2HndOvr/edit"}]})
    members = [db.post(297)] + db.q("SELECT * FROM posts WHERE pair_id=297")
    sound = tx.sound(members)
    assert sound and sound["tubearchivist"] == "JRWI136ytAA" and sound["post_id"] == 295, sound
    assert sound["timeline"] == "video" and sound["size"] > 0 and sound["duration"] == 23.0
    heard = [[1.0, 1.3, "There"], [1.3, 1.5, "is"], [1.5, 2.0, "something"], [2.0, 2.3, "wrong"], [2.3, 2.5, "with"],
             [2.5, 2.8, "this"], [2.8, 3.2, "town."], [4.0, 4.4, "What?"], [8.0, 8.3, "And"], [8.3, 8.5, "no"],
             [8.5, 8.8, "one"], [8.8, 9.2, "seems"], [9.2, 9.4, "to"], [9.4, 9.9, "notice"], [9.9, 10.1, "at"],
             [10.1, 10.4, "all."], [12.0, 12.3, "Okay,"], [12.3, 12.6, "then"], [12.6, 12.8, "let's"],
             [12.8, 13.0, "us"], [13.0, 13.3, "go."]]
    (tmp_path / "heard.json").write_text(json.dumps(heard))
    monkeypatch.setenv("JRWI_FAKE_HEARING", str(tmp_path / "heard.json"))
    scheduler._transcripts(one=297)   # reads the transcript (and, asked for by hand, times it)
    job = client.get("/api/transcripts/jobs/297").json()
    assert job["sound"]["source"] == "tubearchivist" and job["sound"]["url"] == "/api/transcripts/sound/295", job
    every = client.get("/api/transcripts/jobs?all=true").json()
    assert 297 in [j["item_id"] for j in every["jobs"]], every
    # The sound, copied out of TubeArchivist's copy, as another computer gets it
    r = client.get("/api/transcripts/sound/295")
    assert r.status_code == 200 and r.content[:4] == b"\x1aE\xdf\xa3", r.status_code   # a Matroska file
    assert client.get("/api/transcripts/sound/300").status_code == 404   # archived sound: its own address
    # Timed on the server from the copy, which is dropped afterwards
    scheduler._transcripts(one=297)
    row = db.one("SELECT * FROM transcripts WHERE item_id=297")
    assert row["aligned"] and row["timing"]["sound_post"] == 295 and row["timing"]["timeline"] == "video", row
    assert not Path(sound["path"]).exists()
    lines = client.get("/api/transcript/295").json()
    assert lines["aligned"] and lines["lines"][1]["start"] is not None
    # Without TubeArchivist, it's left out, saying why
    tx._ta_seen.clear()
    monkeypatch.setattr(tx, "_ta", lambda: type("T", (), {"configured": False})())
    out = client.get("/api/transcripts/jobs?all=true").json()
    gone = [x for x in out["left_out"] if x["item_id"] == 297]
    assert gone and "only on YouTube" in gone[0]["why"] and "isn't connected" in gone[0]["why"], out["left_out"]
