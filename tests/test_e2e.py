"""End-to-end test against the mock site: one check, then the API and the Jellyfin folder.

Needs ffmpeg for the test media:  sh tests/make_media.sh && python -m pytest tests
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
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
    assert first["author"]["avatar"].startswith("/files/avatars/") and first["author"]["badges"] == []
    # No profile picture: no avatar, and the badges stay badges
    lee = next(c for c in comments["items"] if c["author"]["name"] == "Lee")
    assert lee["author"]["avatar"] is None
    badges = lee["author"]["badges"]
    assert [(b["name"], b["icon"]) for b in badges] == [("4-month member", "🔥"), ("JRWiener", None)]
    assert badges[0]["image"] is None and badges[1]["image"].startswith("/files/badges/")
    assert client.get(badges[1]["image"]).status_code == 200
    pinned = client.get("/api/items/300/comments").json()["items"][0]
    assert pinned["pinned"] and pinned["author"]["name"] == "Kim" and pinned["like_count"] == 327


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
    assert "progress" not in client.get("/api/posts/300").json()   # single posts: ask /api/progress


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
    """Riptide has two episodes numbered 68 (on purpose, to skip 69): both stay, in upload order."""
    from jrwi import organize
    db = app.state.db
    try:
        for pid, day, title in [(9501, "2022-01-07", "The Greatest Form of Flattery | Riptide #68"),
                                (9502, "2022-01-14", "A Stranger at Sea | Riptide #68"),
                                (9503, "2022-01-21", "Next One | Riptide #70")]:
            db.upsert("posts", "id", {"id": pid, "url": f"https://example.invalid/{pid}", "title": title,
                                      "path_kind": "videos", "status": "complete", "tag_ids": ["101"],
                                      "published": day + "T00:00:00+00:00"})
        organize.classify_all(db)
        ids = [i["id"] for i in client.get("/api/shows/riptide/items?tab=episodes&order=asc&page_size=200").json()["items"]]
        mine = [i for i in ids if i >= 9501]
        assert mine == [9501, 9502, 9503]
        assert {db.post(i)["pair_id"] for i in mine} == {None}   # two episodes, not two versions of one
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
    summary = web.get("/admin/files").text
    assert "PDF" in summary and ".pdf" in summary and "what's inside" in summary


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
