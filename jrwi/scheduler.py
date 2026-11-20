"""Runs a check every few hours, and whenever someone presses "Check now"."""

from __future__ import annotations

import logging
import random
import shutil
import threading
from datetime import datetime, timedelta, timezone

from .crawler import Archiver
from .db import Db

log = logging.getLogger("jrwi.scheduler")
FIRST_CHECK_DELAY = timedelta(minutes=2)


class Scheduler:
    def __init__(self, archiver: Archiver, db: Db) -> None:
        self.archiver = archiver
        self.db = db
        self.wake = threading.Event()
        self.full_requested = False
        self.next_check: datetime | None = None
        self.thread = threading.Thread(target=self._loop, name="jrwi-scheduler", daemon=True)

    def start(self) -> None:
        self.thread.start()

    @property
    def running(self) -> bool:
        return self.archiver.status.running

    def check_now(self, full: bool = False) -> bool:
        if self.running:
            return False
        self.full_requested = full
        self.wake.set()
        return True

    def stop(self) -> None:
        self.archiver.stop_event.set()

    def _interval(self) -> timedelta | None:
        hours = float(self.db.setting("check_interval_hours") or 0)
        return timedelta(hours=hours) if hours > 0 else None

    def _plan_next(self, after: datetime) -> None:
        interval = self._interval()
        if interval is None:
            self.next_check = None
            return
        jitter = interval.total_seconds() * random.uniform(-0.1, 0.1)
        self.next_check = after + interval + timedelta(seconds=jitter)

    def _loop(self) -> None:
        last = self.db.one("SELECT finished FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1")
        start = datetime.now(timezone.utc)
        if last and last["finished"]:
            self._plan_next(datetime.fromisoformat(last["finished"]))
            if self.next_check and self.next_check < start + FIRST_CHECK_DELAY:
                self.next_check = start + FIRST_CHECK_DELAY
        else:
            self.next_check = start + FIRST_CHECK_DELAY if self._interval() else None
        while True:
            timeout = None
            if self.next_check:
                timeout = max(0.0, (self.next_check - datetime.now(timezone.utc)).total_seconds())
            woke = self.wake.wait(timeout=min(timeout, 60) if timeout is not None else 60)
            if not woke and (self.next_check is None or datetime.now(timezone.utc) < self.next_check):
                # Pick up interval changes made on the admin page
                if self.next_check is None and self._interval():
                    self._plan_next(datetime.now(timezone.utc))
                continue
            self.wake.clear()
            full, self.full_requested = self.full_requested, False
            try:
                result = self.archiver.run_check(full=full)
                log.info("check finished: %s", result)
            except Exception:
                log.exception("check crashed")
            self._plan_next(datetime.now(timezone.utc))

    def disk(self) -> dict:
        try:
            free = shutil.disk_usage(self.archiver.paths.media).free
        except OSError:
            free = None
        return {"archive_bytes": self.db.setting("archive_bytes"), "free_bytes": free}
