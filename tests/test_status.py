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
    monkeypatch.setattr(archiver.client, "login", lambda *a, **k: seen.append(archiver.status.phase))
    archiver.db.set_setting("site_email", "fan@example.com")
    archiver.db.set_setting("site_password", "hunter2")
    archiver.status.set(phase="reading the feed", detail="page 3, 30 posts")
    archiver.ensure_login()
    assert seen == ["logging in"]
    assert archiver.status.phase == "reading the feed"
    assert archiver.status.detail == "page 3, 30 posts"


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
