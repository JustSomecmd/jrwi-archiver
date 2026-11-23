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


def test_old_raw_comment_html_is_dropped_once(tmp_path):
    """Before 0.10.3 every comment's HTML was kept in the database too. It's emptied once, and the rest stays."""
    from jrwi.db import Db
    db = Db(tmp_path / "jrwi.db")
    db.x("INSERT INTO comments (id, post_id, author_name, body_text, raw) VALUES ('1:1', 1, 'Sam', 'Hi', ?)",
         ("<div>" + "x" * 100_000 + "</div>",))
    db.drop_comment_raw()
    row = db.one("SELECT * FROM comments WHERE id='1:1'")
    assert row["raw"] is None and row["author_name"] == "Sam" and row["body_text"] == "Hi"
    assert db.setting("comments_raw_dropped")
    db.x("UPDATE comments SET raw = 'again' WHERE id='1:1'")
    db.drop_comment_raw()   # once only
    assert db.one("SELECT raw FROM comments WHERE id='1:1'")["raw"] == "again"


def test_checks_cut_short_by_a_stop_or_restart_are_closed(archiver, monkeypatch):
    from jrwi.scheduler import Scheduler
    db = archiver.db
    cut = db.x("INSERT INTO runs (started, result) VALUES ('2026-11-22T17:00:00+00:00', 'running')")
    ok = db.x("INSERT INTO runs (started, finished, result) "
              "VALUES ('2026-11-22T13:00:00+00:00', '2026-11-22T13:05:00+00:00', 'ok')")
    sched = Scheduler(archiver, db)
    monkeypatch.setattr(sched.thread, "start", lambda: None)
    sched.start()   # the archiver starting again: no check can be running yet
    row = db.one("SELECT * FROM runs WHERE id=?", (cut,))
    assert row["result"] == "stopped" and row["finished"] and "stopped while this check ran" in row["message"]
    assert db.one("SELECT * FROM runs WHERE id=?", (ok,))["finished"] == "2026-11-22T13:05:00+00:00"

    # Shutting down while a check runs records it with the time it was cut short
    running = db.x("INSERT INTO runs (started, result) VALUES ('2026-11-22T18:00:00+00:00', 'running')")
    archiver.status.set(running=True)
    sched.shut_down()
    row = db.one("SELECT * FROM runs WHERE id=?", (running,))
    assert row["result"] == "stopped" and row["finished"] and "shut down" in row["message"]
    archiver.status.set(running=False)
    sched.shut_down()   # nothing running: nothing changes
    assert db.one("SELECT COUNT(*) AS n FROM runs WHERE finished IS NULL")["n"] == 0


def test_a_posts_other_versions_are_found_by_an_index(archiver):
    db = archiver.db
    for sql, args in (("SELECT * FROM posts WHERE pair_id=? ORDER BY id", (1,)),
                      ("SELECT COUNT(*) FROM posts WHERE (pair_id=? OR id=?) AND id != ?", (1, 1, 1))):
        plan = " ".join(r[3] for r in db.conn.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "posts_pair" in plan and "SCAN posts" not in plan


def test_transcripts_are_timed_when_the_schedule_says(archiver, monkeypatch):
    from datetime import datetime

    from jrwi.scheduler import Scheduler
    db = archiver.db
    db.set_setting("transcripts_time", True)   # the switch before 0.12.1
    sched = Scheduler(archiver, db)
    assert sched.transcripts_mode() == "always" and db.setting("transcripts_when") == "always"
    db.set_setting("transcripts_time", False)
    assert Scheduler(archiver, db).transcripts_mode() == "always"   # carried over once, then left alone

    def at(h, m=0):   # a local time
        return datetime(2026, 11, 22, h, m)

    db.set_setting("transcripts_from", "22:30")
    db.set_setting("transcripts_until", "06:00")   # past midnight
    assert [sched.in_window(at(h, m)) for h, m in ((22, 29), (22, 30), (2, 0), (5, 59), (6, 0), (12, 0))] == \
        [False, True, True, True, False, False]
    db.set_setting("transcripts_from", "01:00")
    db.set_setting("transcripts_until", "07:00")
    assert [sched.in_window(at(h)) for h in (0, 1, 6, 7)] == [False, True, True, False]
    db.set_setting("transcripts_until", "01:00")   # the same time twice: all day
    assert sched.in_window(at(12))
    db.set_setting("transcripts_until", "07:00")

    inside = {"now": False}
    monkeypatch.setattr(sched, "in_window", lambda at=None: inside["now"])
    db.set_setting("transcripts_when", "off")
    assert not sched.timing_now()
    sched.transcripts_all_now()   # the admin page's Time transcripts now
    assert sched.transcripts_manual and sched.timing_now() and sched.bg_wake.is_set()
    sched.pause_transcripts()
    assert not sched.transcripts_manual and sched.transcripts_paused
    sched.bg_wake.clear()
    sched.bg_stop.clear()   # as the next run does
    sched.transcripts_paused = False

    db.set_setting("transcripts_when", "hours")
    sched.ser = object()
    assert not sched.timing_now()
    # Reading goes on outside the hours; only timing gives way when they end
    assert not sched._background_yield(timing=False) and sched._background_yield(timing=True)
    # Run out of things to do outside the hours (only reading): it starts again when they begin
    sched.transcripts_idle, sched.idle_timing = True, False
    assert not sched._transcripts_due()
    inside["now"] = True
    assert sched.timing_now() and sched._transcripts_due() and not sched._background_yield(timing=True)
    # Run out of things to do while timing: nothing more until the next check finds something new
    sched.idle_timing = True
    assert not sched._transcripts_due()


def test_background_work_runs_alongside_a_check(archiver, monkeypatch):
    import time

    from jrwi.scheduler import Scheduler
    sched = Scheduler(archiver, archiver.db)
    sched.ser = object()
    calls = []

    def run(ser, should_yield, one=None, read_on=True, timing_on=False):
        calls.append({"one": one, "timing": timing_on, "check_running": archiver.status.running,
                      "gives_way": should_yield()})
        return {"read": 0, "timed": 0}
    monkeypatch.setattr(sched.transcripts, "run", run)

    def wait_for(n):
        for _ in range(100):
            if len(calls) >= n:
                return
            time.sleep(0.02)
        raise AssertionError(calls)

    archiver.status.set(running=True)   # a check is running
    sched.next_check = None
    assert sched.transcripts_all_now() and sched.transcript_now(7)   # neither waits for the check
    sched.bg_thread.start()
    wait_for(2)
    # The episode asked for goes first, then everything else, both while the check runs, and nothing gives way
    assert calls[0] == {"one": 7, "timing": True, "check_running": True, "gives_way": False}
    assert calls[1] == {"one": None, "timing": True, "check_running": True, "gives_way": False}
    for _ in range(100):
        if sched.transcripts_idle:
            break
        time.sleep(0.02)
    assert sched.transcripts_idle and not sched.transcripts_manual
    # The check finishes with new posts: the background looks again at once
    sched.kick_background()
    wait_for(3)
    archiver.status.set(running=False)


def test_work_found_while_the_background_runs_isnt_marked_done(archiver, monkeypatch):
    from jrwi.scheduler import Scheduler
    sched = Scheduler(archiver, archiver.db)
    sched.ser = object()

    def run(ser, should_yield, one=None, read_on=True, timing_on=False):
        sched.kick_background()   # a check running alongside saved new posts meanwhile
        return {"read": 0, "timed": 0}
    monkeypatch.setattr(sched.transcripts, "run", run)
    sched.transcripts_all_now()
    sched._transcripts()
    assert not sched.transcripts_idle and sched.transcripts_manual and sched._transcripts_due()
    monkeypatch.setattr(sched.transcripts, "run", lambda *a, **kw: {"read": 0, "timed": 0})
    sched._transcripts()
    assert sched.transcripts_idle and not sched.transcripts_manual and not sched._transcripts_due()


def test_the_record_of_timings_starts_from_what_was_there(tmp_path):
    import json

    from jrwi.db import Db
    db = Db(tmp_path / "a.db")
    timing = {"at": "2026-11-22T21:00:00+00:00", "took": 1605.0, "duration": 11000.0, "placed": 3559, "unplaced": 70,
              "found": 0.79, "model": "small.en", "threads": 4, "peak_mb": 1719}
    db.x("INSERT INTO posts (id, title) VALUES (5, 'Paid In Blood | Riptide #134')")
    db.x("INSERT INTO transcripts (item_id, lines, aligned, timing) VALUES (5, 3629, 1, ?)", (json.dumps(timing),))
    db.x("INSERT INTO transcripts (item_id, lines, aligned, timing) VALUES (6, 10, 0, '{}')")
    db.conn.close()
    db = Db(tmp_path / "a.db")   # 0.13.4's first start
    runs = db.q("SELECT * FROM transcript_runs")
    assert len(runs) == 1 and runs[0]["item_id"] == 5 and runs[0]["title"] == "Paid In Blood | Riptide #134"
    assert runs[0]["timing"]["took"] == 1605.0
    db.conn.close()
    assert len(Db(tmp_path / "a.db").q("SELECT * FROM transcript_runs")) == 1   # only once

    from jrwi.transcripts import record_figures
    f = record_figures(timing)
    assert f["result"] == "timed" and f["speed"] == round(11000 / 1605, 2) and f["threads_text"] == "4 threads"
    assert round(f["placed_share"], 3) == round(3559 / 3629, 3)
    assert record_figures({"at": "x", "error": "only 2% heard", "found": 0.02})["result"] == "mismatch"
    # Lines placed as a colour: deep purple to 40%, the archive's yellow from 95%, white or dark text by contrast
    from jrwi.transcripts import placed_colour
    assert placed_colour(0.1) == placed_colour(0.4) == ("#6a00a8", "#ffffff")
    assert placed_colour(0.95) == placed_colour(1.0) == ("#f4e434", "#1b0833")
    assert placed_colour(0.675)[0] not in ("#6a00a8", "#f4e434")
    assert record_figures({"at": "x", "error": "no sound"})["result"] == "error"
    assert record_figures({})["result"] == "waiting"
    assert record_figures({"at": "x", "threads_used": [4, 6]})["threads_text"] == "4, 6 threads"
    assert record_figures({"at": "x", "threads": 6, "workers": 4, "source": "pc"})["threads_text"] == "4 × 6 threads"
    gpu = record_figures({"at": "x", "device": "cuda", "batch": 16, "source": "pc"})
    assert gpu["threads_text"] == "GPU, batches of 16" and gpu["where"] == "PC"

    from jrwi.transcripts import compare_lines
    a = [{"start": 1.0}, {"start": 5.0}, {"start": None}, {"start": 9.0}, {"start": 20.0}]
    b = [{"start": 1.2}, {"start": 7.5}, {"start": 3.0}, {"start": None}, {"start": 20.0}]
    c = compare_lines(a, b)
    assert c == {"both": 3, "median": 0.2, "p90": 2.5, "within_1s": round(2 / 3, 3), "only_this": 1, "only_other": 1}
