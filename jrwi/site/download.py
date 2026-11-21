"""Downloading files (resumable) and streams (through yt-dlp)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from .client import Client, SiteError
from .parse import AUDIO_EXTS, VIDEO_EXTS, safe_filename

log = logging.getLogger("jrwi.download")


class DownloadError(SiteError):
    pass


class Transient(DownloadError):
    pass


def content_disposition_name(resp: requests.Response) -> str | None:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    return m.group(1).strip() if m else None


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def find_ffmpeg() -> str | None:
    if shutil.which("ffmpeg"):
        return "PATH"
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_exe() -> str | None:
    f = find_ffmpeg()
    return shutil.which("ffmpeg") if f == "PATH" else f


class Downloader:
    def __init__(self, client: Client, max_height: int = 0,
                 progress: Callable[..., None] | None = None) -> None:
        self.client = client
        self.max_height = max_height
        # progress(text, fraction): fraction is how much of the current file is done, 0 to 1, or None
        self.progress = progress or (lambda msg, fraction=None: None)
        self.ffmpeg = find_ffmpeg()

    def file(self, url: str, dest_dir: Path, name: str | None, *, referer: str, method: str = "GET",
             fields=None, reject_html: bool = False, tries: int = 6) -> Path:
        for attempt in range(4):
            try:
                return self._once(url, dest_dir, name, referer=referer, method=method, fields=fields,
                                  reject_html=reject_html, tries=tries)
            except Transient as e:
                if attempt == 3:
                    raise DownloadError(f"{e}; it will resume on the next check")
                log.info("%s; resuming in %ss", e, 10 * (attempt + 1))
                time.sleep(10 * (attempt + 1))
        raise DownloadError("unreachable")

    def _once(self, url: str, dest_dir: Path, name: str | None, *, referer: str, method: str, fields,
              reject_html: bool, tries: int) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        headers = {"Referer": referer, "Origin": self.client.base}
        dest = part = None
        if name:
            dest = dest_dir / safe_filename(name)
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            part = dest.with_name(dest.name + ".part")
            if part.exists() and part.stat().st_size > 0 and method == "GET":
                headers["Range"] = f"bytes={part.stat().st_size}-"
        r = self.client.request(method, url, headers=headers, data=fields or None, stream=True,
                                pace=self.client.same_site(url), tries=tries)
        try:
            if r.status_code == 416 and part is not None and part.exists():
                os.replace(part, dest)
                return dest
            if r.status_code >= 400:
                raise DownloadError(f"HTTP {r.status_code}")
            if reject_html and "text/html" in r.headers.get("Content-Type", ""):
                raise DownloadError("got a web page instead of a file (it may be private)")
            if dest is None:
                guess = content_disposition_name(r) or os.path.basename(unquote(urlparse(r.url).path)) or "download"
                dest = dest_dir / safe_filename(guess)
                if dest.exists() and dest.stat().st_size > 0:
                    return dest
                part = dest.with_name(dest.name + ".part")
            resumed = r.status_code == 206
            done = part.stat().st_size if (resumed and part.exists()) else 0
            length = int(r.headers.get("Content-Length") or 0)
            total = done + length if length else 0
            shown = time.monotonic()
            try:
                with open(part, "ab" if resumed else "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if self.client.should_stop():
                            raise DownloadError("stopped")
                        f.write(chunk)
                        done += len(chunk)
                        if total and time.monotonic() - shown > 2:
                            shown = time.monotonic()
                            self.progress(f"{dest.name}: {done * 100 // total}% of {human_bytes(total)}", done / total)
            except requests.RequestException as e:
                raise Transient(f"connection dropped ({type(e).__name__})")
            if total and done < total:
                raise Transient(f"download cut off at {done} of {total} bytes")
            os.replace(part, dest)
            return dest
        finally:
            r.close()

    def stream(self, url: str, dest_dir: Path, base: str, *, referer: str, audio_only: bool) -> Path:
        """Download an HLS stream (or anything yt-dlp understands) into dest_dir/base.<ext>."""
        try:
            import yt_dlp
        except ImportError:
            raise DownloadError("yt-dlp isn't installed")
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = safe_filename(base, keep_ext=False)
        for existing in dest_dir.glob(glob_escape(base) + ".*"):
            if existing.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS and existing.stat().st_size > 0:
                return existing
        h = self.max_height
        if audio_only:
            fmt = "ba/b"
        elif h:
            fmt = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        else:
            fmt = "bv*+ba/b"
        progress = self.progress

        class Logger:
            def debug(self, msg):
                if msg.startswith("[download]") and "%" in msg:
                    progress(msg.replace("[download]", "").strip())

            def info(self, msg):
                pass

            def warning(self, msg):
                if "ffprobe not found" not in msg:
                    log.warning("yt-dlp: %s", msg)

            def error(self, msg):
                log.error("yt-dlp: %s", msg)

        should_stop = self.client.should_stop

        last_shown = [0.0]

        def hook(d):
            if should_stop():
                raise DownloadError("stopped")
            if d.get("status") != "downloading" or time.monotonic() - last_shown[0] < 2:
                return
            last_shown[0] = time.monotonic()
            frac = None
            if d.get("fragment_count"):
                frac = min(1.0, (d.get("fragment_index") or 0) / d["fragment_count"])
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if frac is None and total:
                frac = min(1.0, (d.get("downloaded_bytes") or 0) / total)
            kind = "audio" if audio_only or (d.get("info_dict") or {}).get("vcodec") == "none" else "video"
            size = f" of ~{human_bytes(total)}" if total else ""
            progress(f"{kind} {round(frac * 100) if frac is not None else '?'}%{size}", frac)

        opts = {
            "outtmpl": str(dest_dir / (base + ".%(ext)s")),
            "format": fmt,
            "merge_output_format": "mp4",
            "http_headers": {"Referer": referer, "Origin": self.client.base,
                             "User-Agent": self.client.session.headers["User-Agent"]},
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
            "continuedl": True,
            "overwrites": False,
            "quiet": True,
            "noprogress": True,
            "logger": Logger(),
            "progress_hooks": [hook],
        }
        if self.ffmpeg and self.ffmpeg != "PATH":
            opts["ffmpeg_location"] = self.ffmpeg
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadError:
            raise
        except Exception as e:
            raise DownloadError(f"stream download failed: {str(e)[:300]}")
        downloads = (info or {}).get("requested_downloads") or []
        if downloads and downloads[0].get("filepath") and Path(downloads[0]["filepath"]).exists():
            return Path(downloads[0]["filepath"])
        for existing in dest_dir.glob(glob_escape(base) + ".*"):
            if existing.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS:
                return existing
        raise DownloadError("yt-dlp finished but no file was found")


def probe_height(path: Path) -> int | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
        return int(out.stdout.strip().split("\n")[0]) if out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def to_jpeg(src: Path, dest: Path) -> bool:
    """Convert an image (usually WebP) to JPEG for Jellyfin. Returns False if ffmpeg isn't there."""
    exe = ffmpeg_exe()
    if not exe or not src.exists():
        return False
    try:
        subprocess.run([exe, "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1", "-q:v", "3", str(dest)],
                       check=True, timeout=60)
        return dest.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def to_poster(src: Path, dest: Path, width: int = 1000, height: int = 1500) -> bool:
    """A 2:3 JPEG poster from a picture of any shape: the whole picture, centred on its own average colour."""
    exe = ffmpeg_exe()
    if not exe or not src.exists():
        return False
    try:
        avg = subprocess.run([exe, "-loglevel", "error", "-i", str(src), "-frames:v", "1", "-vf", "scale=1:1:flags=area",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True, timeout=60).stdout
        colour = "0x" + (avg[:3].hex() if len(avg) >= 3 else "111320")
        fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={colour}")
        subprocess.run([exe, "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", fit, "-q:v", "3",
                        str(dest)], check=True, timeout=60)
        return dest.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def human_bytes(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
