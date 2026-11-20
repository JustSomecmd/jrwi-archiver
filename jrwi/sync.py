"""Lengths of video and audio files, and how far an episode's video and audio version are apart.

The audio version of an episode often starts a little earlier or later than the video (an intro or a sponsor read
that one has and the other doesn't). To carry a position from one to the other, the archiver compares the video's
soundtrack with the audio version: both are turned into a loudness curve (100 points a second), and the shift where
the curves match best is the offset. A second, later stretch is then compared on its own; only when it finds the
same shift is the offset trusted ("checked"). Otherwise the offset is 0 and not checked, and positions are carried
over unchanged.

Offsets follow the contract: audio position = video position + offset.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np

from .site.download import ffmpeg_exe

log = logging.getLogger(__name__)

RATE = 100          # loudness curve: points per second (10 ms each)
FIRST = 600.0       # seconds of each file compared at the start
MAX_SHIFT = 300.0   # the furthest apart the two versions may start, in seconds
CHECK = 120.0       # seconds compared again later on
AGREE = 0.25        # the second comparison must find the same shift within this many seconds


def probe_duration(path: Path) -> float | None:
    """A media file's length in seconds."""
    if not path or not Path(path).is_file():
        return None
    probe = shutil.which("ffprobe")
    try:
        if probe:
            out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                                 capture_output=True, text=True, timeout=120)
            value = json.loads(out.stdout or "{}").get("format", {}).get("duration")
            if value not in (None, "N/A"):
                return round(float(value), 2)
        exe = ffmpeg_exe()
        if exe:
            out = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=120)
            for line in out.stderr.splitlines():
                line = line.strip()
                if line.startswith("Duration:"):
                    h, m, s = line.split(",")[0].split()[1].split(":")
                    return round(int(h) * 3600 + int(m) * 60 + float(s), 2)
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return None


def loudness(path: Path, start: float, length: float) -> np.ndarray:
    """A file's loudness from `start` for `length` seconds, RATE points a second, on a log scale."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg is needed to compare video and audio")
    sr = 8000
    cmd = [exe, "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{length:.3f}", "-i", str(path), "-vn",
           "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=1800).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    step = sr // RATE
    n = len(x) // step
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = x[: n * step].astype(np.float64).reshape(n, step)
    return np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-4)


def best_shift(a: np.ndarray, v: np.ndarray, max_shift: int) -> tuple[int, float, float]:
    """The shift k (in points) where v[n + k] matches a[n] best, within ±max_shift.

    The curves are compared by how the loudness changes from one point to the next, which ignores differences in
    overall volume and gives a sharp peak at the right shift. Returns (k, score, runner_up): score is the
    correlation at k (up to 1); runner_up is the best correlation at least a second away from k."""
    if len(a) < RATE * 5 or len(v) < RATE * 5:
        return 0, 0.0, 0.0
    a, v = np.diff(a), np.diff(v)
    a = (a - a.mean()) / (a.std() or 1.0)
    v = (v - v.mean()) / (v.std() or 1.0)
    size = 1 << (len(a) + len(v)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(v, size) * np.conj(np.fft.rfft(a, size)), size)
    shifts = np.arange(-max_shift, max_shift + 1)
    raw = corr[shifts % size]
    # How many points overlap at each shift, so long and short overlaps compare fairly
    overlap = np.minimum(len(a), len(v) - shifts) - np.maximum(0, -shifts)
    scores = np.where(overlap > RATE * 5, raw / np.maximum(overlap, 1), -1.0)
    i = int(np.argmax(scores))
    away = np.abs(np.arange(len(scores)) - i) > RATE
    runner_up = float(scores[away].max()) if away.any() else 0.0
    return int(shifts[i]), float(scores[i]), runner_up


def confident(score: float, runner_up: float) -> bool:
    """A clear match: a good correlation that stands well above every other shift."""
    return score >= 0.25 and score - runner_up >= 0.15


def find_offset(video: Path, audio: Path, video_len: float | None = None, audio_len: float | None = None) -> dict:
    """How far the audio version is from the video's soundtrack. Returns {"offset": seconds, "checked": bool,
    "score": float}; offset is 0 and checked False when the two can't be lined up with confidence."""
    video_len = video_len or probe_duration(video) or 0
    audio_len = audio_len or probe_duration(audio) or 0
    unsure = {"offset": 0.0, "checked": False, "score": 0.0}
    if min(video_len, audio_len) < 10:
        return unsure
    first = min(FIRST, audio_len, video_len)
    max_shift = int(min(MAX_SHIFT, first / 2) * RATE)
    a = loudness(audio, 0, first + max_shift / RATE)
    v = loudness(video, 0, first + max_shift / RATE)
    k, score, runner_up = best_shift(a, v, max_shift)
    shift = k / RATE                    # video position = audio position + shift
    if not confident(score, runner_up):
        log.info("video and audio don't line up clearly (best %.2f, next best %.2f)", score, runner_up)
        return {**unsure, "score": round(score, 3)}
    # Check later on: a stretch of the audio from the middle should match the video at the same shift
    if audio_len > first + CHECK + 60:
        mid = audio_len / 2
        window = 15.0
        a2 = loudness(audio, mid, CHECK)
        v2 = loudness(video, mid + shift - window, CHECK + 2 * window)
        # v2 starts `window` seconds before where the audio's stretch should be, so k2 should be about +window
        k2, score2, runner_up2 = best_shift(a2, v2, int(2 * window * RATE))
        shift2 = shift + (k2 / RATE - window)
        if not confident(score2, runner_up2) or abs(shift2 - shift) > AGREE:
            log.info("video and audio matched at the start (%.2fs) but not later on (%.2fs)", -shift, -shift2)
            return {**unsure, "score": round(score, 3)}
    return {"offset": round(-shift, 2), "checked": True, "score": round(score, 3)}
