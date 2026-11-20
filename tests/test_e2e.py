"""End-to-end test against the mock site: one check, then the API and the Jellyfin folder.

Needs ffmpeg for the test media:  sh tests/make_media.sh && python -m pytest tests
"""

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
    JRWI_YT_THUMBS=f"http://127.0.0.1:{PORT}/imgproxy/yt",
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
    assert zombie.index("Sam") < zombie.index("Jo") and "Lee" not in zombie and "View 1 reply" in zombie
    rest = web.get("/items/299/comments-html?csort=top&offset=2&limit=10")
    assert "Lee" in rest.text and rest.headers["X-More"] == "0"
    newest = web.get("/items/299/comments-html?csort=new&offset=0&limit=2")
    assert newest.headers["X-More"] == "1"


def test_jellyfin_layout():
    media = Path(os.environ["JRWI_MEDIA"])
    assert (media / "Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-300].mp4").exists()
    assert (media / "Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-300].nfo").exists()
    assert (media / "Riptide/tvshow.nfo").exists()
    assert any(media.glob("Riptide/Season 00/*[[]jrwi-297[]].mp4"))


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
