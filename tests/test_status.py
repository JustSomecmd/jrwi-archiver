"""The admin page's status: phases, login messages and scheduled checks without a login."""

import tempfile
from pathlib import Path

import pytest

from jrwi.config import Paths
from jrwi.crawler import Archiver
from jrwi.db import Db
from jrwi.site.client import LoginFailed


@pytest.fixture()
def archiver(monkeypatch):
    monkeypatch.delenv("JRWI_EMAIL", raising=False)
    monkeypatch.delenv("JRWI_PASSWORD", raising=False)
    root = Path(tempfile.mkdtemp(prefix="jrwi-status-"))
    paths = Paths(data=root / "data", archive=root / "archive", media=root / "media")
    for d in (paths.data, paths.archive, paths.media):
        d.mkdir(parents=True)
    return Archiver(Db(paths.data / "jrwi.db"), paths)


def test_phase_comes_back_after_logging_in(archiver, monkeypatch):
    seen = []
    monkeypatch.setattr(archiver.client, "login",
                        lambda *a, **k: seen.append((archiver.status.step, archiver.status.phase)))
    archiver.db.set_setting("site_email", "fan@example.com")
    archiver.db.set_setting("site_password", "hunter2")
    archiver.status.begin("feed")
    archiver.status.set(detail="Page 3: 30 posts read, 0 new")
    archiver.ensure_login()
    assert seen == [("login", "Logging in to jrwishow.com")]
    assert (archiver.status.step, archiver.status.phase) == ("feed", "Looking for new posts in the members feed")
    assert archiver.status.detail == "Page 3: 30 posts read, 0 new"


def test_login_message_without_and_with_an_old_session(archiver):
    with pytest.raises(LoginFailed) as e:
        archiver.ensure_login()
    assert "run out" not in str(e.value)
    archiver.client.cookie_file.write_text("[]")
    with pytest.raises(LoginFailed) as e:
        archiver.ensure_login()
    assert "run out" in str(e.value)


def test_has_login(archiver):
    assert not archiver.has_login()
    archiver.db.set_setting("site_email", "fan@example.com")
    archiver.db.set_setting("site_password", "hunter2")
    assert archiver.has_login()


def test_scheduled_check_waits_for_a_login(archiver, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from jrwi import scheduler as sched

    ran = []
    monkeypatch.setattr(archiver, "run_check", lambda full=False: ran.append(full) or {"result": "ok"})
    s = sched.Scheduler(archiver, archiver.db)
    monkeypatch.setattr(sched, "FIRST_CHECK_DELAY", timedelta(seconds=0))
    s.start()
    import time
    time.sleep(1.5)
    assert ran == []                                   # due, but nothing to log in with
    assert s.next_check > datetime.now(timezone.utc)   # planned for later instead
    assert archiver.db.one("SELECT COUNT(*) AS n FROM runs")["n"] == 0
    s.check_now()                                      # Check now still runs, to show the problem
    time.sleep(1.5)
    assert ran == [False]


def test_next_check_is_known_while_a_check_runs(archiver, monkeypatch):
    """The next check is planned when a check starts, so while one runs, next_check is the next one (an interval
    ahead), not the time this one was planned for."""
    import time
    from datetime import datetime, timedelta, timezone

    from jrwi import scheduler as sched

    archiver.db.set_setting("check_interval_hours", 4)
    archiver.db.set_setting("site_email", "fan@example.com")
    archiver.db.set_setting("site_password", "hunter2")
    s = sched.Scheduler(archiver, archiver.db)
    during = []
    monkeypatch.setattr(archiver, "run_check", lambda full=False: during.append(s.next_check) or {"result": "ok"})
    monkeypatch.setattr(sched, "FIRST_CHECK_DELAY", timedelta(seconds=0))
    s.start()
    time.sleep(1.5)
    now = datetime.now(timezone.utc)
    assert len(during) == 1                                   # the first check ran
    assert during[0] > now + timedelta(hours=3.5)             # and knew the next one while it ran
    assert s.next_check == during[0]                          # which stays planned after it ends

def test_reparse_after_an_update(archiver, monkeypatch):
    from jrwi import scheduler as sched
    from jrwi.site.parse import PARSER_VERSION

    calls = []
    monkeypatch.setattr(archiver, "run_reparse", lambda: calls.append(1) or {})
    s = sched.Scheduler(archiver, archiver.db)
    s._reparse_after_update()                 # nothing archived yet: just remember the version
    assert calls == [] and archiver.db.setting("parser_version") == PARSER_VERSION
    archiver.db.upsert("posts", "id", {"id": 1, "title": "x", "status": "complete"})
    archiver.db.set_setting("parser_version", PARSER_VERSION - 1)
    s._reparse_after_update()                 # archived with older rules: re-read once
    assert calls == [1]


def comment_html(when: str) -> str:
    return ('<turbo-frame id="comment_9"><div class="comment" data-testid="Post.Comment">'
            '<div class="comment__aside"><div class="avatar">a</div></div>'
            '<div data-testid="Post.CommentAuthor">Ann</div><span class="comment__date">' + when + '</span>'
            '<div class="comment__body"><p>Hi</p></div></div></turbo-frame>')


def test_comment_dates_count_from_when_the_page_was_saved():
    from jrwi.site.parse import parse_comments
    c = parse_comments([(comment_html("13d"), None, "2026-01-20T12:00:00+00:00")], "https://jrwishow.com/supporters")[0]
    assert c["date"] == "2026-01-07" and c["date_text"] == "13d"


def test_comment_keeps_its_most_exact_date(archiver):
    store = lambda when, at: archiver.store_comments(5, [(comment_html(when), None, at)], "https://jrwishow.com/supporters")
    one = lambda: archiver.db.one("SELECT date, date_text FROM comments WHERE id='5:9'")
    store("11h", "2026-01-20T12:00:00+00:00")
    assert one() == {"date": "2026-01-20", "date_text": "11h"}
    store("2w", "2026-02-01T12:00:00+00:00")      # weeks later the site only says "2w": keep the exact day
    assert one() == {"date": "2026-01-20", "date_text": "11h"}
    store("3d", "2026-01-23T08:00:00+00:00")      # still rougher than hours
    assert one() == {"date": "2026-01-20", "date_text": "11h"}
    store("10h", "2026-01-20T11:00:00+00:00")     # as exact: replaces it, as when re-reading the same page
    assert one() == {"date": "2026-01-20", "date_text": "10h"}


def test_new_episode_in_the_podcast_feed_starts_a_check(archiver, monkeypatch):
    """Between checks the scheduler reads the podcast feed when it's due, and a new episode in it starts a check
    right away; without a login to check with, it waits for Settings."""
    import time

    from jrwi import scheduler as sched

    archiver.db.set_setting("podcast_feed", "https://jrwishow.com/supporters/podcast/key/rss")
    s = sched.Scheduler(archiver, archiver.db)
    ran, reads = [], []
    answers = iter([{"start_check": True}, {"start_check": True}])
    monkeypatch.setattr(archiver, "run_check", lambda full=False: ran.append(full) or {"result": "ok"})
    monkeypatch.setattr(s, "read_feed", lambda force=False: reads.append(1) or next(answers, {"start_check": False}))
    monkeypatch.setattr(s, "feed_due", lambda: len(reads) < 3)
    monkeypatch.setattr(s.wake, "wait", lambda timeout=None: time.sleep(0.02) or False)   # minutes pass quickly
    s.start()
    time.sleep(0.5)
    assert len(reads) >= 1 and ran == []        # nothing to log in with yet
    archiver.db.set_setting("site_email", "fan@example.com")
    archiver.db.set_setting("site_password", "hunter2")
    reads.clear()
    answers = iter([{"start_check": True}])
    monkeypatch.setattr(s, "read_feed", lambda force=False: reads.append(1) or next(answers, {"start_check": False}))
    time.sleep(0.5)
    assert ran == [False]                       # one ordinary check, started by the feed
    assert s.next_check is not None             # and the scheduled one moved on from it
