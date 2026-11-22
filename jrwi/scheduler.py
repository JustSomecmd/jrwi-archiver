"""Runs a check every few hours, and whenever someone presses "Check now"."""

from __future__ import annotations

import logging
import random
import shutil
import threading
from datetime import datetime, timedelta, timezone

from . import memory, podcast
from .crawler import Archiver, Status
from .transcripts import ModelProblem, Transcripts, Yielded
from .site.parse import PARSER_VERSION
from .db import Db, now as now_iso

log = logging.getLogger("jrwi.scheduler")
FIRST_CHECK_DELAY = timedelta(minutes=2)
# Shown with a check that couldn't finish because the archiver stopped while it ran
SHUT_DOWN = ("Cut short: the archiver was shut down while this check ran (an update, or the container stopping). "
             "The next check picks up what it didn't finish.")
RESTARTED = ("Cut short: the archiver stopped while this check ran (an update, a restart or a crash). "
             "The next check picks up what it didn't finish.")


class Scheduler:
    def __init__(self, archiver: Archiver, db: Db) -> None:
        self.archiver = archiver
        self.db = db
        self.wake = threading.Event()
        self.full_requested = False
        self.reparse_requested = False
        self.check_requested = False
        self.next_check: datetime | None = None
        self.planned_with: timedelta | None = None   # the interval next_check was planned with
        self.last_start: datetime | None = None
        self.feed_read: datetime | None = None
        self.feed_lock = threading.Lock()
        # Work in the background between checks, which anything else takes over from (timing transcripts, v0.23)
        self.bg = Status()
        self.bg_stop = threading.Event()
        self.transcripts = Transcripts(db, archiver.paths, self.bg)
        self.ser = None                      # the web app's Serializer, which finds an item's posts
        self.transcript_requested = None     # an item to time now, from the admin page
        self.transcripts_idle = False        # nothing left to do in the background until the next check
        self.transcripts_paused = False      # paused by hand until the next check
        self.thread = threading.Thread(target=self._loop, name="jrwi-scheduler", daemon=True)

    def start(self) -> None:
        # No check can be running yet, so one without an end was cut short when the archiver last stopped
        # (killed, or a crash), as checks run in this thread, which ends with the process
        if self.db.close_unfinished_runs(RESTARTED):
            log.info("a check was cut short when the archiver last stopped; it's recorded as stopped")
        self.thread.start()

    def shut_down(self) -> None:
        """The archiver is stopping (an update, or the container stopping): a check that's running can't finish,
        so it's recorded as cut short now, with the right time. Should it end in the moments left, it records
        its own result over this."""
        if self.running:
            self.db.close_unfinished_runs(SHUT_DOWN)

    @property
    def running(self) -> bool:
        return self.archiver.status.running

    def check_now(self, full: bool = False) -> bool:
        if self.running:
            return False
        self.full_requested = full
        self.check_requested = True
        self.wake.set()
        return True

    def reparse_now(self) -> bool:
        """Re-read saved pages next, in the scheduler's thread so it never overlaps a check."""
        if self.running:
            return False
        self.reparse_requested = True
        self.wake.set()
        return True

    def _reparse(self) -> None:
        try:
            result = self.archiver.run_reparse()
            log.info("re-read saved pages: %s", result)
            memory.release()
        except Exception:
            log.exception("re-reading saved pages failed")

    def _reparse_after_update(self) -> None:
        """After an update that reads pages differently, re-read everything once before the next check."""
        if str(self.db.setting("parser_version") or "") == str(PARSER_VERSION):
            return
        if self.db.one("SELECT 1 FROM posts LIMIT 1") is None:
            self.db.set_setting("parser_version", PARSER_VERSION)
            return
        log.info("this version reads saved pages differently; re-reading them")
        self._reparse()

    def stop(self) -> None:
        self.archiver.stop_event.set()

    # ------------------------------------------------------------------ transcripts, in the background

    def transcript_now(self, item_id: int | None) -> bool:
        """Time one episode's transcript next (the newest not timed yet when None). False while a check runs."""
        if self.running:
            return False
        self.transcript_requested = item_id if item_id is not None else 0
        self.transcripts_paused = False
        self.wake.set()   # what runs in the background gives way to it
        return True

    def pause_transcripts(self) -> None:
        self.transcripts_paused = True
        self.bg_stop.set()

    def _transcripts_due(self) -> bool:
        if self.ser is None or self.transcripts_idle or self.transcripts_paused or self.wake.is_set():
            return False
        if not (self.db.setting("transcripts_read") or self.db.setting("transcripts_time")):
            return False
        if self.feed_due():
            return False
        now = datetime.now(timezone.utc)
        return not (self.next_check and now >= self.next_check - timedelta(minutes=1))

    def _background_yield(self) -> bool:
        now = datetime.now(timezone.utc)
        return (self.wake.is_set() or self.bg_stop.is_set() or self.feed_due()
                or bool(self.next_check and now >= self.next_check))

    def _transcripts(self, one: int | None = None) -> None:
        """Read new transcripts and time them, until something else needs the scheduler. one: an item asked for
        on the admin page (0 for the newest not timed yet), which only a check or Pause interrupts."""
        if self.ser is None:
            return
        self.bg_stop.clear()
        self.bg.set(running=True, started=now_iso())
        should_yield = (lambda: self.wake.is_set() or self.bg_stop.is_set()) if one is not None else self._background_yield
        try:
            if one == 0:
                one = self.transcripts.newest_untimed(self.transcripts.items(self.ser))
                if one is None:
                    return
            done = self.transcripts.run(self.ser, should_yield, one=one,
                                        read_on=bool(self.db.setting("transcripts_read")) or one is not None,
                                        timing_on=bool(self.db.setting("transcripts_time")))
            if one is None:
                self.transcripts_idle = True
            if done["read"] or done["timed"]:
                log.info("transcripts: read %s, timed %s", done["read"], done["timed"])
        except Yielded:
            log.info("transcripts: giving way; what was heard so far is kept")
        except ModelProblem as e:
            log.warning("transcripts: %s", e)
            self.db.set_setting("transcripts_problem", str(e)[:300])
            self.transcripts_idle = True   # tried again after the next check
        except Exception:
            log.exception("transcripts failed")
            self.transcripts_idle = True
        finally:
            self.bg.set(running=False, step="idle", phase="idle", detail="", done=0, total=0, current=None, unit=None)
            memory.release()

    def feed_due(self) -> bool:
        minutes = float(self.db.setting("feed_minutes") or 0)
        if minutes <= 0 or not (self.db.setting("podcast_feed") or "").strip():
            return False
        return self.feed_read is None or datetime.now(timezone.utc) - self.feed_read >= timedelta(minutes=minutes)

    def read_feed(self, force: bool = False) -> dict:
        """Read the podcast feed now (never during a check, as the check reads the site anyway)."""
        with self.feed_lock:
            self.feed_read = datetime.now(timezone.utc)
            try:
                return podcast.poll(self.db, force=force)
            except Exception:
                log.exception("reading the podcast feed failed")
                return {"ok": False, "error": "reading the feed failed; see the log", "start_check": False}

    def _interval(self) -> timedelta | None:
        hours = float(self.db.setting("check_interval_hours") or 0)
        return timedelta(hours=hours) if hours > 0 else None

    def _plan_next(self, after: datetime) -> None:
        """The next check comes one interval (give or take 10%) after `after`: the start of the last check, so the
        time shown stays right while a check runs, instead of being the time that check was planned for."""
        interval = self._interval()
        self.planned_with = interval
        if interval is None:
            self.next_check = None
            return
        jitter = interval.total_seconds() * random.uniform(-0.1, 0.1)
        self.next_check = after + interval + timedelta(seconds=jitter)

    def _loop(self) -> None:
        try:
            self.db.drop_comment_raw()
        except Exception:
            log.exception("tidying the database failed")
        self._reparse_after_update()
        last = self.db.one("SELECT started FROM runs WHERE started IS NOT NULL ORDER BY id DESC LIMIT 1")
        start = datetime.now(timezone.utc)
        if last and last["started"]:
            self.last_start = datetime.fromisoformat(last["started"])
            self._plan_next(self.last_start)
            if self.next_check and self.next_check < start + FIRST_CHECK_DELAY:
                self.next_check = start + FIRST_CHECK_DELAY
        else:
            self.next_check = start + FIRST_CHECK_DELAY if self._interval() else None
            self.planned_with = self._interval()
        while True:
            if self._transcripts_due():
                self._transcripts()
            timeout = None
            if self.next_check:
                timeout = max(0.0, (self.next_check - datetime.now(timezone.utc)).total_seconds())
            woke = self.wake.wait(timeout=min(timeout, 60) if timeout is not None else 60)
            from_feed = False
            if not woke and (self.next_check is None or datetime.now(timezone.utc) < self.next_check):
                # Pick up a new interval set in Settings
                if self._interval() != self.planned_with:
                    now = datetime.now(timezone.utc)
                    self._plan_next(self.last_start or now)
                    if self.next_check and self.next_check < now + FIRST_CHECK_DELAY:
                        self.next_check = now + FIRST_CHECK_DELAY
                # The podcast feed has an episode the archive hasn't found: the site most likely has it too
                if not (self.feed_due() and self.read_feed().get("start_check") and self.archiver.has_login()):
                    continue
                from_feed = True
                log.info("the podcast feed has a new episode; checking the site")
            manual = woke or self.wake.is_set() or from_feed
            self.wake.clear()
            if self.transcript_requested is not None:
                one, self.transcript_requested = self.transcript_requested, None
                self._transcripts(one=one)
                continue
            if self.reparse_requested:
                self.reparse_requested = False
                self._reparse()
                self.transcripts_idle = False
                if self.check_requested:
                    self.wake.set()   # Check now was pressed while the re-read was being started: it's next
                continue
            if not manual and not self.archiver.has_login():
                # Nothing to log in with yet: wait for Settings instead of recording a failed check
                self._plan_next(datetime.now(timezone.utc))
                continue
            full, self.full_requested = self.full_requested, False
            self.check_requested = False
            self.last_start = datetime.now(timezone.utc)
            self._plan_next(self.last_start)
            try:
                result = self.archiver.run_check(full=full)
                log.info("check finished: %s", result)
            except Exception:
                log.exception("check crashed")
            try:
                podcast.match(self.db)   # the feed's new episodes, now that the check has saved their posts
            except Exception:
                log.exception("matching the podcast feed failed")
            memory.release()
            self.transcripts_idle = self.transcripts_paused = False   # new posts may have transcripts
            # A check that ran past the next one's time: the next comes a whole interval after this one ended
            now = datetime.now(timezone.utc)
            if self.next_check and self.next_check < now + timedelta(minutes=10):
                self._plan_next(now)

    def disk(self) -> dict:
        try:
            free = shutil.disk_usage(self.archiver.paths.media).free
        except OSError:
            free = None
        return {"archive_bytes": self.db.setting("archive_bytes"), "free_bytes": free}
