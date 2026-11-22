"""Where things live, and the settings the admin page can change."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    data: Path      # database, cookies, logs
    archive: Path   # one folder per post, pages, assets
    media: Path     # video in a layout Jellyfin reads as TV shows

    @classmethod
    def from_env(cls) -> "Paths":
        p = cls(
            data=Path(os.environ.get("JRWI_DATA", "/data")),
            archive=Path(os.environ.get("JRWI_ARCHIVE", "/archive")),
            media=Path(os.environ.get("JRWI_MEDIA", "/media/jrwi")),
        )
        for d in (p.data, p.archive, p.media):
            d.mkdir(parents=True, exist_ok=True)
        return p


SITE_BASE = os.environ.get("JRWI_SITE", "https://jrwishow.com").rstrip("/")
MUX_BASE = os.environ.get("JRWI_MUX_BASE", "https://stream.mux.com").rstrip("/")
YT_THUMBS = os.environ.get("JRWI_YT_THUMBS", "https://i.ytimg.com").rstrip("/")
GDOCS_BASE = os.environ.get("JRWI_GDOCS_BASE", "https://docs.google.com").rstrip("/")
DRIVE_DOWNLOAD = os.environ.get("JRWI_DRIVE_DOWNLOAD", "https://drive.usercontent.google.com/download")
# The page Google makes for showing a shared folder inside another site: a plain list of what the folder holds
DRIVE_FOLDER_VIEW = os.environ.get("JRWI_DRIVE_FOLDER_VIEW", "https://drive.google.com/embeddedfolderview")

# Defaults for everything the admin page can change. Stored in the settings table.
DEFAULTS: dict = {
    "site_email": "",
    "site_password": "",
    "check_interval_hours": 4,
    "video": True,
    "audio": True,
    "images": True,
    "attachments": True,
    "comments": True,
    "google_docs": True,
    "public_pages": True,
    "youtube_embeds": False,       # TubeArchivist is the better place for JRWI's YouTube channel
    "max_height": 0,               # 0 = best available
    "comment_refresh_days": 30,    # re-read comments on posts newer than this every check
    "request_delay": 1.5,          # seconds between page requests to the site
    "notify_url": "",              # ntfy topic URL or Discord webhook
    "public_url": "",              # how the app and podcast apps reach this server, for the RSS feed
    "ta_url": "",                  # TubeArchivist, such as http://tubearchivist:8000
    "ta_token": "",                # its API token (its Settings page)
    "handover": True,              # delete an archived video once TubeArchivist has the same video from YouTube
    "recheck_days": 60,            # each check reads the feed back this far, to catch posts added with an older date
    "podcast_feed": "",            # the members' private podcast feed (RSS); its address holds a key, like a password
    "feed_minutes": 15,            # how often to read it; 0 = never
    "watched_minutes_left": 2,     # an episode counts as watched with this little left (contract v0.22)
    "api_token": "",
    "web_password_hash": "",
    "session_secret": "",
}


def new_token() -> str:
    return secrets.token_urlsafe(32)
