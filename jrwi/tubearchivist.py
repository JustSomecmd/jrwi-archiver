"""Asking TubeArchivist whether it has a YouTube video, and where its file is.

The archiver only reads from TubeArchivist: GET /api/ping/, GET /api/video/<id>/ and the video's file under its
media_url, each with "Authorization: Token <key>". It never changes anything there.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class TaError(Exception):
    """TubeArchivist couldn't be asked: not reachable, a wrong token, or an answer that isn't TubeArchivist's."""


class TubeArchivist:
    def __init__(self, url: str, token: str, timeout: float = 20) -> None:
        self.url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout

    @classmethod
    def from_settings(cls, s: dict) -> "TubeArchivist":
        return cls(s.get("ta_url") or "", s.get("ta_token") or "")

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Token {self.token}"}

    def _get(self, path: str) -> requests.Response:
        try:
            return requests.get(f"{self.url}/{path.lstrip('/')}", headers=self.headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise TaError(f"TubeArchivist can't be reached at {self.url} ({type(e).__name__})") from None

    def ping(self) -> str:
        """Checks the address and token. Returns a short message; raises TaError when they don't work."""
        r = self._get("/api/ping/")
        if r.status_code in (401, 403):
            raise TaError("TubeArchivist turned the API token down")
        if r.status_code != 200:
            raise TaError(f"TubeArchivist answered HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise TaError("that address doesn't answer like TubeArchivist") from None
        if body.get("response") != "pong":
            raise TaError("that address doesn't answer like TubeArchivist")
        return "connected" + (f", TubeArchivist {body['version']}" if body.get("version") else "")

    def video(self, youtube_id: str) -> dict | None:
        """TubeArchivist's video, or None when it doesn't have it (yet). Only downloaded videos are in its index."""
        r = self._get(f"/api/video/{youtube_id}/")
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise TaError("TubeArchivist turned the API token down")
        if r.status_code != 200:
            raise TaError(f"TubeArchivist answered HTTP {r.status_code} for {youtube_id}")
        try:
            body = r.json()
        except ValueError:
            raise TaError("TubeArchivist's answer wasn't JSON") from None
        video = body.get("data", body) if isinstance(body, dict) else None   # older versions wrap it in "data"
        if not isinstance(video, dict) or not video.get("youtube_id"):
            return None
        return video

    @staticmethod
    def duration(video: dict) -> float | None:
        d = (video.get("player") or {}).get("duration")
        try:
            return float(d) if d else None
        except (TypeError, ValueError):
            return None

    def file_url(self, video: dict) -> str | None:
        """The address of the video's file in TubeArchivist, which takes the same token."""
        media = video.get("media_url")
        if not media:
            return None
        return media if media.startswith("http") else f"{self.url}/{media.lstrip('/')}"
