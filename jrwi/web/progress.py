"""Where you are in an episode, shared between its video and its audio version (contract v0.3 and v0.9).

Each episode (item) has one saved position and one watched state. It can be read and saved through any of its
posts: the video post gives and takes positions in the video's timeline, the audio post in the audio's. Positions
are converted with the item's sync offset (audio position = video position + offset), so pausing the video at
1:00:00 and opening the audio continues at the same moment.
"""

from __future__ import annotations

from ..db import Db, now
from .serialize import Serializer


class Progress:
    def __init__(self, db: Db, ser: Serializer) -> None:
        self.db = db
        self.ser = ser

    def _item(self, post: dict):
        primary = self.ser.primary_of(post["id"]) or post
        members = self.ser.members(primary)
        return primary, members, self.ser._video(members), self.ser._audio(members)

    @staticmethod
    def _timeline(post_id: int, video: dict | None, audio: dict | None) -> str | None:
        if audio and audio["post_id"] == post_id:
            return "audio"
        if video and video["post_id"] == post_id:
            return "video"
        return None

    @staticmethod
    def _convert(position: float, src: str | None, dest: str | None, offset: float) -> float:
        if src == "video" and dest == "audio":
            return position + offset
        if src == "audio" and dest == "video":
            return position - offset
        return position

    def get(self, post: dict) -> dict:
        primary, members, video, audio = self._item(post)
        found = self.read(post["id"], primary, members, video, audio)
        if found:
            return found
        part = {"video": video, "audio": audio}.get(self._timeline(post["id"], video, audio)) or {}
        return {"post_id": post["id"], "position": 0.0, "duration": part.get("duration"), "finished": False,
                "updated_at": None}

    def of_item(self, primary: dict, members: list[dict], video: dict | None, audio: dict | None) -> dict | None:
        """The item's progress in its main post's timeline (contract v0.10), or None when it was never played."""
        return self.read(primary["id"], primary, members, video, audio)

    def read(self, post_id: int, primary: dict, members: list[dict], video: dict | None,
             audio: dict | None) -> dict | None:
        timeline = self._timeline(post_id, video, audio)
        part = {"video": video, "audio": audio}.get(timeline) or {}
        duration = part.get("duration")
        ids = [m["id"] for m in members]
        marks = ",".join("?" * len(ids))
        # One row per item; older ones may still sit under one of its other posts
        row = self.db.one(f"SELECT * FROM progress WHERE post_id IN ({marks}) ORDER BY updated_at DESC LIMIT 1", ids)
        if not row:
            return None
        saved_in = row["timeline"]
        if saved_in is None:  # saved before v0.9: in the timeline of the post it was saved under
            saved_in = self._timeline(row["post_id"], video, audio) or "audio"
        sync = self.ser.sync_of(primary, video, audio)
        position = self._convert(row["position"] or 0.0, saved_in, timeline, sync["offset"] if sync else 0.0)
        if duration:
            position = min(position, duration)
        if saved_in == timeline and duration is None:
            duration = row["duration"]
        return {"post_id": post_id, "position": round(max(0.0, position), 2), "duration": duration,
                "finished": bool(row["finished"]), "updated_at": row["updated_at"]}

    def set(self, post: dict, body: dict) -> dict:
        primary, members, video, audio = self._item(post)
        timeline = self._timeline(post["id"], video, audio)
        ids = [m["id"] for m in members if m["id"] != primary["id"]]
        with self.db.lock:
            if ids:
                self.db.x(f"DELETE FROM progress WHERE post_id IN ({','.join('?' * len(ids))})", ids)
            self.db.upsert("progress", "post_id", {
                "post_id": primary["id"], "position": max(0.0, float(body.get("position") or 0)),
                "duration": body.get("duration"), "finished": bool(body.get("finished")),
                "timeline": timeline, "updated_at": now()})
        return self.get(post)
