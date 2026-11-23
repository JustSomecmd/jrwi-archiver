"""Timed transcripts (contract v0.23).

A transcript is a PDF of a post whose name says it's one: a linked Google Doc or Drive file ("Episode transcription
here. [1RkmhusZ].pdf") or an attachment. The ones JRWI posts are Word files opened in Google Docs, laid out the same
way: each speaker's name in bold on a line of its own, then what they said; notes in brackets ("[laughter]") inside a
speech or on a line of their own. They give no times.

Reading turns the PDF into lines by that layout. Timing hears the episode once with faster-whisper, which gives the
words it heard with their times, and matches the transcript's words to them: runs of words that agree pin the lines
down, and a line whose words weren't heard goes right after the line before it, or gets no time when it sits by a long
gap, where there's no telling which side it belongs on. Mishearing a word costs little, as lines only need some of
their words found, so a small, quick model does.

Reading and timing run in a child process (python -m jrwi.transcripts), one episode at a time, so the speech model's
memory (about 1.5 GB for small.en) goes back to the system the moment it's done and the service itself never loads it.
Hearing goes in ten-minute pieces, each saved as it's done, so a check, the podcast feed or Stop can take over at any
time and the episode carries on from there later.
"""

from __future__ import annotations

import bisect
import gzip
import json
import logging
import os
import queue
import re
import resource
import subprocess
import sys
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

log = logging.getLogger("jrwi.transcripts")

NAMED = re.compile(r"transcri(?:pt|ption)", re.I)
NOTE = re.compile(r"\[[^\]]*\]")
NOTE_ONLY = re.compile(r"^\s*(\[[^\]]*\]\s*)+$")
LEADING_NOTE = re.compile(r"^\s*\[[^\]]*\]")
COLON_NAME = re.compile(r"^([A-Z][\w .'’()&-]{0,40}?):\s+(\S.*)$")
SPLIT = re.compile(r"[\s\-‐-―/]+")

CHUNK = 600.0        # seconds of sound heard at a time
OVERLAP = 10.0       # heard twice where two pieces meet, so no word is cut in half
# How the episode is heard, here and on another computer (contract v0.24: a job hands these over, so a PC hears the
# same way and its words match the same way)
HEARING = {"chunk": CHUNK, "overlap": OVERLAP, "language": "en", "beam_size": 1, "word_timestamps": True,
           "vad_filter": True, "condition_on_previous_text": False}
MAX_IMPORT_WORDS = 400_000   # far more than a long episode has (about 35,000 words in three hours)
PACE = 0.3           # seconds a word usually takes, to go from a heard word back to its line's first
LONG_GAP = 10.0      # a line with nothing heard, next to a longer gap, gets no time
MODELS = ("base.en", "small.en", "medium.en")
VERSION = 1          # of how lines are read and matched: a change re-reads, and re-matches what was heard


# ------------------------------------------------------------------ finding

def find(members: list[dict], archive: Path) -> dict | None:
    """The item's transcript: a PDF whose name says transcript, from its audio post first, then its main post,
    then its other posts. None when it has none, or it isn't saved."""
    def rank(i_m):
        i, m = i_m
        return (0 if (m.get("media") or {}).get("audio") else 1 if i == 0 else 2, i)
    for _, m in sorted(enumerate(members), key=rank):
        for entry in list(m.get("documents") or []) + list(m.get("attachments") or []):
            name, rel = entry.get("name") or "", entry.get("url") or ""
            if not rel or not NAMED.search(name) or not name.lower().endswith(".pdf"):
                continue
            rel = unquote(rel.removeprefix("/files/"))
            path = archive / rel
            if path.is_file():
                return {"post_id": m["id"], "path": rel, "name": name, "size": path.stat().st_size}
    return None


# ------------------------------------------------------------------ reading (in the child)

def _physical_lines(pdf):
    for pno, page in enumerate(pdf.pages, 1):
        lines = []
        for w in page.extract_words(extra_attrs=["fontname"], use_text_flow=True):
            bold = "bold" in (w.get("fontname") or "").lower()
            if lines and abs(w["top"] - lines[-1]["top"]) <= 2:
                line = lines[-1]
                line["text"] += " " + w["text"]
                line["x1"] = w["x1"]
                line["bold"] = line["bold"] and bold
            else:
                lines.append({"top": w["top"], "bottom": w["bottom"], "x1": w["x1"], "text": w["text"],
                              "bold": bold, "page": pno})
        page.close()   # pdfplumber keeps every page otherwise: 470 MB for 359 pages rather than 55
        yield from lines


def _paragraphs(pdf):
    """Lines joined into paragraphs: wrapped lines sit closer together than paragraphs do, and a name is bold."""
    para, prev = None, None
    for line in _physical_lines(pdf):
        if para is None or line["bold"] != para["bold"]:
            starts = True
        elif line["page"] != prev["page"]:
            # Over a page break the paragraph goes on, unless the last line ended short and this one opens with a note
            starts = prev["x1"] < 480 and bool(LEADING_NOTE.match(line["text"]))
        else:
            starts = line["top"] - prev["bottom"] > 8
        if starts:
            if para:
                yield para
            para = {"text": line["text"], "bold": line["bold"]}
        else:
            para["text"] += " " + line["text"]
        prev = line
    if para:
        yield para


def read_pdf(path: Path) -> list[dict]:
    """A transcript's lines: {"speaker", "text"}, a note or a group's line ("[all the players] Whoa!") of its own
    with speaker None. A transcript without bold names is read as "Name: what they said", or else line by line."""
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        paras = list(_paragraphs(pdf))
    out, speaker, turn = [], None, None
    if any(p["bold"] for p in paras):
        for p in paras:
            text = p["text"].strip()
            if p["bold"]:
                speaker, turn = text, None
            elif NOTE_ONLY.match(text) or (turn is not None and LEADING_NOTE.match(text)):
                out.append({"speaker": None, "text": text})
                turn = None
            elif turn is None:
                turn = {"speaker": speaker, "text": text}
                out.append(turn)
            else:
                turn["text"] += "\n" + text
        return out
    for p in paras:
        text = p["text"].strip()
        m = COLON_NAME.match(text)
        out.append({"speaker": m.group(1).strip(), "text": m.group(2).strip()} if m else {"speaker": None, "text": text})
    return out


# ------------------------------------------------------------------ matching

def norm(word: str) -> str:
    w = word.lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"[^a-z0-9']", "", w).strip("'")


def tokens(text: str) -> list[str]:
    """The words of a line as they'd be heard: notes in brackets aren't spoken."""
    return [t for t in (norm(w) for w in SPLIT.split(NOTE.sub(" ", text))) if t]


def heard_tokens(words: list) -> list[tuple[str, float, float]]:
    """Heard words as [start, end, word], split the way the transcript's are, each part with its share of the time."""
    out = []
    for start, end, word in words:
        parts = [t for t in (norm(w) for w in SPLIT.split(word)) if t]
        for k, t in enumerate(parts):
            step = (end - start) / len(parts)
            out.append((t, start + k * step, start + (k + 1) * step))
    return out


def _lis(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest run of pairs (sorted by their first) whose seconds go up too."""
    tails: list[int] = []
    at: list[int] = []
    prev: list[int | None] = [None] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        pos = bisect.bisect_left(tails, j)
        if pos == len(tails):
            tails.append(j)
            at.append(k)
        else:
            tails[pos], at[pos] = j, k
        prev[k] = at[pos - 1] if pos else None
    out, k = [], (at[-1] if at else None)
    while k is not None:
        out.append(pairs[k])
        k = prev[k]
    return out[::-1]


def _blocks(a: list[str], b: list[str]) -> list[tuple[int, int, int]]:
    """Runs of words two long word lists share, in order, as (i, j, n). Three words in a row that occur once in each
    pin the lists to each other, as patience diff does, so a phrase said many times ("okay", "I think") can't pull
    a stretch out of place; the stretches between are matched as difflib does."""
    def unique_triples(seq: list[str], lo: int, hi: int) -> dict:
        seen: dict = {}
        for k in range(lo, hi - 2):
            g = (seq[k], seq[k + 1], seq[k + 2])
            seen[g] = None if g in seen else k
        return {g: k for g, k in seen.items() if k is not None}

    out: list[tuple[int, int, int]] = []

    def fill(alo: int, ahi: int, blo: int, bhi: int, depth: int) -> None:
        if alo >= ahi or blo >= bhi:
            return
        if (ahi - alo) * (bhi - blo) > 4_000_000 and depth < 4:
            ua, ub = unique_triples(a, alo, ahi), unique_triples(b, blo, bhi)
            anchors = _lis(sorted((ua[g], ub[g]) for g in ua.keys() & ub.keys()))
            if anchors:
                ca, cb = alo, blo
                for i, j in anchors:
                    if i < ca or j < cb:
                        continue
                    fill(ca, i, cb, j, depth + 1)
                    out.append((i, j, 3))
                    ca, cb = i + 3, j + 3
                fill(ca, ahi, cb, bhi, depth + 1)
                return
        for i, j, n in SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False).get_matching_blocks():
            if n:
                out.append((alo + i, blo + j, n))

    fill(0, len(a), 0, len(b), 0)
    merged: list[tuple[int, int, int]] = []
    for i, j, n in sorted(out):
        if merged and merged[-1][0] + merged[-1][2] == i and merged[-1][1] + merged[-1][2] == j:
            merged[-1] = (merged[-1][0], merged[-1][1], merged[-1][2] + n)
        else:
            merged.append((i, j, n))
    return merged


def match(lines: list[dict], words: list) -> dict:
    """Each line's start and end from the words heard. Returns {"lines": [...], "placed", "unplaced", "found"}:
    "found" is the share of the transcript's words that were heard, which says whether it's this episode's."""
    ref = [(t, i) for i, line in enumerate(lines) for t in tokens(line["text"])]
    hyp = heard_tokens(words)
    out = [dict(line, start=None, end=None) for line in lines]
    if not ref or not hyp:
        return {"lines": out, "placed": 0, "unplaced": len(lines), "found": 0.0}
    blocks = _blocks([t for t, _ in ref], [t for t, *_ in hyp])
    at: list[tuple[float, float] | None] = [None] * len(ref)
    for a, b, n in blocks:
        if n >= 2:   # two words or more in a row agree: an anchor
            for k in range(n):
                at[a + k] = hyp[b + k][1:]
    anchored = [k for k in range(len(ref)) if at[k] is not None]
    for a, b, n in blocks:
        if n == 1 and at[a] is None:
            # One word alone counts only where it falls between the anchors around it, at a likely pace
            j = bisect.bisect(anchored, a)
            lo = at[anchored[j - 1]][0] if j > 0 else float("-inf")
            hi = at[anchored[j]][0] if j < len(anchored) else float("inf")
            span = anchored[j] - anchored[j - 1] if 0 < j < len(anchored) else 50
            if lo <= hyp[b][1] <= hi and hi - lo < span * 1.5 + 30:
                at[a] = hyp[b][1:]
    found = sum(1 for x in at if x is not None) / len(ref)
    first_ref: dict[int, int] = {}
    last_ref: dict[int, int] = {}
    for k, (_, i) in enumerate(ref):
        first_ref.setdefault(i, k)
        last_ref[i] = k
    first: dict[int, float] = {}
    last: dict[int, float] = {}
    for k, (_, i) in enumerate(ref):
        if at[k] is not None:
            if i not in first:
                first[i] = max(0.0, at[k][0] - (k - first_ref[i]) * PACE)
            last[i] = at[k][1] + (last_ref[i] - k) * PACE
    n = len(out)
    starts: list[float | None] = [None] * n
    ends: list[float | None] = [None] * n
    prev = 0.0
    for i in range(n):   # starts in order: a line can't begin before the one above it
        if i in first:
            starts[i] = max(first[i], prev)
            ends[i] = max(starts[i], last[i])
            prev = starts[i]
    next_known: list[float | None] = [None] * n
    upcoming = None
    for i in range(n - 1, -1, -1):
        next_known[i] = upcoming
        if starts[i] is not None:
            upcoming = starts[i]
    prev_end = 0.0
    for i in range(n):
        if starts[i] is None:
            # Nothing of it heard: right after the line before, unless a long gap follows, where it can't be told
            # which side it's on, so it's shown without a time
            at = prev_end + 0.3 if i else 0.0
            nxt = next_known[i]
            if nxt is not None and nxt - at > LONG_GAP:
                continue
            if nxt is not None:
                at = min(at, nxt)
            starts[i] = ends[i] = at
        prev_end = ends[i]
    for i in range(n):
        if starts[i] is not None:
            nxt = next_known[i]
            end = ends[i] if nxt is None else min(ends[i], max(starts[i], nxt))
            out[i]["start"], out[i]["end"] = round(starts[i], 2), round(end, 2)
    placed = sum(1 for x in out if x["start"] is not None)
    return {"lines": out, "placed": placed, "unplaced": len(out) - placed, "found": round(found, 3),
            "words": len(ref)}


# ------------------------------------------------------------------ hearing (in the child)

def _decode(audio: Path, start: float, length: float):
    """A piece of the sound as 16 kHz mono floats, as Whisper takes it: about 38 MB for ten minutes."""
    import numpy as np
    from .site.download import ffmpeg_exe
    exe = ffmpeg_exe() or "ffmpeg"
    raw = subprocess.run([exe, "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(audio),
                          "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


PROGRESS_EVERY = 5.0   # seconds between the hearing's "how far" lines within a piece


class Pace:
    """How long the episode being heard has left, from how fast it has gone since hearing began (this time: an
    episode carried on from a saved piece counts from there, and loading the model doesn't count)."""

    def __init__(self) -> None:
        self.since: float | None = None
        self.share: float = 0.0

    def reset(self) -> None:
        self.since = None

    def left(self, share: float, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        if self.since is None:
            self.since, self.share = now, share
            return None
        took, gained = now - self.since, share - self.share
        if took < 60 or gained < 0.005:
            return None   # too soon to tell
        return took * (1 - share) / gained


def left_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute left"
    if minutes < 60:
        return f"about {minutes} min left"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min left" if minutes else f"about {hours} h left"


def _emit(msg: dict) -> None:
    msg["rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def hear(audio: Path, total: float, start_at: float, model_name: str, threads: int, models_dir: Path,
         hotwords: str | None) -> None:
    """Hear the sound from start_at to the end, a piece at a time, writing each piece's words as a line of JSON."""
    fake = os.environ.get("JRWI_FAKE_HEARING")
    if fake:   # tests: the words come from a file, in the pieces they'd come in
        words = json.loads(Path(fake).read_text())
        t = start_at
        while t < total:
            _emit({"from": t, "to": min(t + CHUNK, total),
                   "words": [w for w in words if t <= w[0] < t + CHUNK]})
            t += CHUNK
        return
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    from faster_whisper import WhisperModel
    _emit({"loading": model_name})
    began = time.monotonic()
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads,
                         download_root=str(models_dir))
    _emit({"ready": round(time.monotonic() - began, 1)})
    t = start_at
    while t < total - 0.5:
        length = min(CHUNK + OVERLAP, total - t)
        lo = t + OVERLAP / 2 if t > 0 else 0.0
        hi = t + CHUNK + OVERLAP / 2 if t + CHUNK < total else float("inf")
        segments, _ = model.transcribe(_decode(audio, t, length), language=HEARING["language"],
                                       beam_size=HEARING["beam_size"], word_timestamps=True,
                                       vad_filter=HEARING["vad_filter"],
                                       condition_on_previous_text=HEARING["condition_on_previous_text"],
                                       hotwords=hotwords or None)
        words = []
        told = time.monotonic()
        for seg in segments:   # heard as they're asked for, so how far it's got can be told as it goes
            for w in seg.words or []:
                s = t + w.start
                if lo <= s < hi and w.word.strip():
                    words.append([round(s, 2), round(t + w.end, 2), w.word.strip()])
            if time.monotonic() - told >= PROGRESS_EVERY:
                _emit({"at": round(min(t + seg.end, t + CHUNK, total), 1)})
                told = time.monotonic()
        _emit({"from": t, "to": min(t + CHUNK, total), "words": words})
        t += CHUNK


def _main(argv: list[str]) -> int:
    cmd = argv[0]
    if cmd == "read":
        lines = read_pdf(Path(argv[1]))
        Path(argv[2]).write_text(json.dumps(lines, ensure_ascii=False))
        _emit({"lines": len(lines)})
    elif cmd == "match":
        lines = json.loads(Path(argv[1]).read_text())
        words = json.loads(gzip.decompress(Path(argv[2]).read_bytes()))["words"]
        result = match(lines, words)
        Path(argv[3]).write_text(json.dumps(result, ensure_ascii=False))
        _emit({k: result[k] for k in ("placed", "unplaced", "found")})
    elif cmd == "hear":
        audio, total, start_at, model_name, threads, models_dir = argv[1:7]
        hear(Path(audio), float(total), float(start_at), model_name, int(threads), Path(models_dir),
             argv[7] if len(argv) > 7 else None)
    return 0


# ------------------------------------------------------------------ the job (in the service)

class Yielded(Exception):
    """Told to give way (Pause, another episode asked for, the end of the hours set): what was heard so far is
    kept, and the episode carries on later."""


class SoundChanged(Exception):
    """The file being heard changed or went while it was heard, as a check running alongside can do (downloading
    it again, moving it in the Jellyfin folder, handing the video over to TubeArchivist). Not the episode's fault:
    nothing is recorded, and it's tried again with what's there next time."""


class ModelProblem(Exception):
    """The speech model couldn't be downloaded or loaded: no episode is to blame, so the job stops."""


class Transcripts:
    def __init__(self, db, paths, status) -> None:
        self.db = db
        self.paths = paths
        self.status = status
        self.dir = paths.archive / "transcripts"

    # files
    def lines_file(self, item_id: int) -> Path:
        return self.dir / f"{item_id}.json.gz"

    def heard_file(self, item_id: int) -> Path:
        return self.dir / f"{item_id}.heard.json.gz"

    @staticmethod
    def _write(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False).encode(), 6))
        tmp.replace(path)

    @staticmethod
    def _load(path: Path):
        try:
            return json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            return None

    def has(self, item_id: int) -> bool:
        return self.db.one("SELECT 1 FROM transcripts WHERE item_id=?", (item_id,)) is not None

    def row(self, item_id: int) -> dict | None:
        return self.db.one("SELECT * FROM transcripts WHERE item_id=?", (item_id,))

    def threads(self) -> int:
        try:
            n = int(self.db.setting("transcript_threads") or 0)
        except (TypeError, ValueError):
            n = 0
        return n if n > 0 else max(1, min(8, (os.cpu_count() or 2) // 2))

    def model(self) -> str:
        m = self.db.setting("transcript_model") or "small.en"
        return m if m in MODELS else "small.en"

    # the sound to hear
    def sound(self, members: list[dict]) -> dict | None:
        """The archived audio, or else the archived video's sound: {path, timeline, post_id, size, duration}."""
        for part, root in (("audio", self.paths.archive), ("video", self.paths.media)):
            for m in members:
                media = (m.get("media") or {}).get(part)
                if media and media.get("path") and (root / media["path"]).is_file():
                    f = root / media["path"]
                    return {"path": str(f), "timeline": part, "post_id": m["id"], "size": f.stat().st_size,
                            "duration": media.get("duration")}
        return None

    def items(self, ser) -> list[tuple[dict, list[dict], dict]]:
        """Every item with a transcript, newest first."""
        out = []
        for primary in self.db.q("SELECT * FROM posts WHERE pair_id IS NULL AND status IN ('complete', 'partial') "
                                 "ORDER BY published DESC, id DESC"):
            members = ser.members(primary)
            doc = find(members, self.paths.archive)
            if doc:
                out.append((primary, members, doc))
        return out

    # children
    def _child(self, args: list[str], on_line: Callable[[dict], None] | None, should_yield: Callable[[], bool]) -> dict:
        """Run python -m jrwi.transcripts with args at a low priority, handing each line it writes to on_line.
        Stops it when should_yield() says so (Yielded). Returns the last line."""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS=str(self.threads()),
                   HF_HOME=str(self.paths.data / "models" / "hf"), HF_HUB_DISABLE_TELEMETRY="1")
        proc = subprocess.Popen([sys.executable, "-m", "jrwi.transcripts", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env,
                                preexec_fn=lambda: os.nice(10))
        lines: queue.Queue = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout] and None, daemon=True)
        reader.start()
        errors: list[str] = []
        err_reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
        err_reader.start()
        last: dict = {}
        try:
            while True:
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    if proc.poll() is not None and not reader.is_alive():
                        break
                    if should_yield():
                        raise Yielded()
                    continue
                try:
                    last = json.loads(line)
                except ValueError:
                    continue
                if on_line:
                    on_line(last)
                if should_yield():
                    raise Yielded()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        proc.wait()
        err_reader.join(timeout=2)
        if proc.returncode:
            tail = "".join(errors).strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
            raise RuntimeError(tail[0][:300])
        return last

    # reading
    def read(self, primary: dict, doc: dict, should_yield) -> dict:
        tmp = self.dir / f"{primary['id']}.lines.tmp"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._child(["read", str(self.paths.archive / doc["path"]), str(tmp)], None, should_yield)
        lines = json.loads(tmp.read_text())
        tmp.unlink(missing_ok=True)
        self._write(self.lines_file(primary["id"]), {"version": VERSION, "document": doc, "aligned": False,
                                                     "timeline": None, "lines": [dict(l, start=None, end=None)
                                                                                 for l in lines]})
        self.db.upsert("transcripts", "item_id", {
            "item_id": primary["id"], "document": doc["path"], "doc_size": doc["size"], "lines": len(lines),
            "aligned": 0, "timing": {}, "updated_at": _now()})
        return {"lines": len(lines)}

    # timing
    def time(self, primary: dict, members: list[dict], should_yield, progress=None) -> dict:
        """Hear the item's sound (carrying on from what was heard before) and time its lines."""
        item_id = primary["id"]
        stored = self._load(self.lines_file(item_id)) or {}
        lines = [{"speaker": l["speaker"], "text": l["text"]} for l in stored.get("lines") or []]
        sound = self.sound(members)
        if not lines or not sound:
            raise RuntimeError("no transcript lines" if not lines else "no archived audio or video to hear")
        total = float(sound["duration"] or 0)
        if not total:
            from .sync import probe_duration
            total = float(probe_duration(Path(sound["path"])) or 0)
        if not total:
            raise RuntimeError("the sound's length isn't known")
        model = self.model()
        key = {"path": sound["path"], "size": sound["size"], "model": model}
        heard = self._load(self.heard_file(item_id)) or {}
        if heard.get("key") != key:
            heard = {"key": key, "until": 0.0, "words": [], "took": 0.0, "peak_mb": 0}
        heard_now = heard["until"] < total - 0.5   # or only matched again, from what was heard before
        names = sorted({l["speaker"] for l in lines if l["speaker"]})[:40]
        hot = " ".join(names)[:400] or None
        if heard["until"] < total - 0.5:
            began = time.monotonic()
            # For the record: when hearing began, how often it was started (carrying on after a pause counts), and
            # every thread count it ran with, as the setting can change between pieces
            heard["started"] = heard.get("started") or _now()
            heard["sessions"] = int(heard.get("sessions") or 0) + 1
            if self.threads() not in heard.setdefault("threads_used", []):
                heard["threads_used"].append(self.threads())

            def piece(msg: dict) -> None:
                heard["peak_mb"] = max(heard.get("peak_mb") or 0, int(msg.get("rss_mb") or 0))
                if "words" in msg:
                    heard["words"].extend(msg["words"])
                    heard["until"] = msg["to"]
                    heard["took"] = round(heard.get("took", 0.0) + (time.monotonic() - piece.since), 1)
                    piece.since = time.monotonic()
                    self._write(self.heard_file(item_id), heard)
                    if progress:
                        progress(heard["until"] / total, "hearing")
                elif "at" in msg and progress:
                    progress(max(heard["until"], float(msg["at"])) / total, "hearing")
                elif "loading" in msg and progress:
                    progress(heard["until"] / total, "loading the speech model (downloaded once, the first time)")
                elif "ready" in msg:
                    heard["load"] = msg["ready"]
                    piece.ready = True
                    piece.since = time.monotonic()
            piece.since = began
            piece.ready = bool(os.environ.get("JRWI_FAKE_HEARING"))
            if progress:
                progress(heard["until"] / total, "hearing")
            try:
                self._child(["hear", sound["path"], str(total), str(heard["until"]), model, str(self.threads()),
                             str(self.paths.data / "models"), *([hot] if hot else [])], piece, should_yield)
            except RuntimeError as e:
                if not _same_file(sound):
                    raise SoundChanged(sound["path"]) from None
                if not piece.ready:
                    raise ModelProblem(f"the speech model {model} couldn't be loaded: {e}") from None
                raise
        if not _same_file(sound):
            raise SoundChanged(sound["path"])
        if progress:
            progress(1.0, "matching the lines to what was heard")
        result, match_took = self._match(item_id, lines, self.heard_file(item_id), should_yield)
        aligned = self._aligned(result, lines)
        timing = {"source": "server", "timeline": sound["timeline"], "sound_post": sound["post_id"],
                  "duration": round(total, 1),
                  "sound_size": sound["size"], "model": model, "threads": (heard.get("threads_used") or
                                                                             [self.threads()])[-1],
                  "threads_used": heard.get("threads_used") or [self.threads()], "took": heard["took"],
                  "load": heard.get("load"), "peak_mb": heard["peak_mb"], "started": heard.get("started"),
                  "sessions": heard.get("sessions") or 1, "match_took": match_took, "heard_now": heard_now,
                  "heard": len(heard["words"]), "words": result.get("words"), "found": result["found"],
                  "placed": result["placed"], "unplaced": result["unplaced"], "at": _now(),
                  "error": None if aligned else self._mismatch(result)}
        self._store(primary, stored, lines, sound, result, aligned, timing)
        self.record(primary, timing)
        return timing

    def _match(self, item_id: int, lines: list, heard_path: Path, should_yield=lambda: False) -> tuple[dict, float]:
        """Match the transcript's lines to the words in a heard file, in a child process. (result, seconds)"""
        src = self.dir / f"{item_id}.match-in.json"
        dest = self.dir / f"{item_id}.match-out.json"
        src.write_text(json.dumps(lines, ensure_ascii=False))
        began = time.monotonic()
        try:
            self._child(["match", str(src), str(heard_path), str(dest)], None, should_yield)
            return json.loads(dest.read_text()), round(time.monotonic() - began, 1)
        finally:
            src.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)

    @staticmethod
    def _aligned(result: dict, lines: list) -> bool:
        # A transcript that isn't this episode's (a link to another one) matches next to nothing
        return result["found"] >= 0.3 and result["placed"] >= 0.5 * max(1, len(lines))

    @staticmethod
    def _mismatch(result: dict) -> str:
        return (f"only {round(result['found'] * 100)}% of the transcript's words were heard, so it doesn't seem to "
                "be this episode's")

    def _store(self, primary: dict, stored: dict, lines: list, sound: dict, result: dict, aligned: bool,
               timing: dict) -> None:
        """Keep the timed lines (or, when the transcript isn't this episode's, the lines without times)."""
        self._write(self.lines_file(primary["id"]), {"version": VERSION, "document": stored.get("document"),
                                                     "aligned": aligned, "timeline": sound["timeline"],
                                                     "lines": result["lines"] if aligned else
                                                     [dict(l, start=None, end=None) for l in lines]})
        self.db.update("transcripts", "item_id", primary["id"], {"aligned": int(aligned), "timing": timing,
                                                                 "updated_at": _now()})

    # hearing on another computer (contract v0.24, every episode since v0.25)
    def job(self, primary: dict, members: list[dict], runs: list[dict] | None = None) -> dict | None:
        """What another computer needs to hear this episode as the archiver would: where its sound is, how to hear
        it, and the speakers' names to listen for, with which models have heard it already. None without read
        lines or sound."""
        stored = self._load(self.lines_file(primary["id"])) or {}
        lines = stored.get("lines") or []
        sound = self.sound(members)
        if not lines or not sound:
            return None
        duration = sound["duration"]
        if not duration:
            from .sync import probe_duration
            duration = probe_duration(Path(sound["path"]))
        names = sorted({l["speaker"] for l in lines if l.get("speaker")})[:40]
        row = self.row(primary["id"]) or {}
        timing = row.get("timing") or {}
        current = timing if timing.get("at") and timing.get("sound_size") == sound["size"] else {}
        part = sound["timeline"]
        return {"item_id": primary["id"], "title": primary.get("title"), "lines": len(lines),
                "timed": bool(row.get("aligned")),
                "timed_with": current.get("model") if current.get("found") is not None else None,
                "found": current.get("found"), "source": current.get("source") if current else None,
                "heard_with": self.heard_with(primary["id"], sound["size"], runs),
                "sound": {"post_id": sound["post_id"], "timeline": part, "size": sound["size"],
                          "duration": round(float(duration), 1) if duration else None,
                          "url": f"/api/{part}/{sound['post_id']}", "name": Path(sound["path"]).name},
                "hotwords": " ".join(names)[:400] or None, "hearing": dict(HEARING), "archiver": VERSION}

    def heard_with(self, item_id: int, size: int, runs: list[dict] | None = None) -> list[str]:
        """The models that have heard this sound to the end and been matched, here or on another computer."""
        if runs is None:
            runs = [r["timing"] for r in self.runs(item_id)]
        return sorted({t["model"] for t in runs if t and t.get("model") and t.get("found") is not None
                       and t.get("sound_size", size) == size})

    def jobs(self, ser, limit: int | None = 20, every: bool = False) -> dict:
        """Episodes as jobs, newest first: those not timed yet, or with every, every episode whose transcript has
        been read and that has sound, timed or not. left_out says which episodes with a transcript can't be
        jobs, and why."""
        found = self.items(ser)
        runs: dict[int, list] = {}
        for r in self.db.q("SELECT item_id, timing FROM transcript_runs"):
            runs.setdefault(r["item_id"], []).append(r["timing"])
        out, left_out = [], []
        for primary, members, _ in (found if every else self.to_time(found)):
            if limit is not None and len(out) >= limit:
                break
            j = self.job(primary, members, runs.get(primary["id"], []))
            if j:
                out.append(j)
            elif every:
                why = ("it has no archived audio or video to hear" if not self.sound(members) else
                       "its transcript hasn't been read into lines yet (Settings > Transcripts > Read transcripts)")
                left_out.append({"item_id": primary["id"], "title": primary.get("title"), "why": why})
        return {"jobs": out, "left_out": left_out, "episodes": len(found)}

    def import_heard(self, primary: dict, members: list[dict], payload: dict) -> dict:
        """Time an episode with words heard on another computer: each result (one per model) is matched as the
        archiver's own would be, every one is recorded, and the one that found the most of the transcript's words
        is kept. Two or more are compared. Raises ValueError when it can't be used."""
        item_id = primary["id"]
        stored = self._load(self.lines_file(item_id)) or {}
        lines = [{"speaker": l["speaker"], "text": l["text"]} for l in stored.get("lines") or []]
        sound = self.sound(members)
        if not lines:
            raise ValueError("this episode's transcript hasn't been read")
        if not sound:
            raise ValueError("this episode has no archived audio or video")
        if int(payload.get("sound_size") or 0) != sound["size"]:
            raise ValueError("this episode's sound isn't the one that was heard: it changed since, or the result is "
                             "for another episode")
        results = payload.get("results")
        if not isinstance(results, list) or not results or len(results) > 6:
            raise ValueError("there are no results in it")
        self.dir.mkdir(parents=True, exist_ok=True)
        tries = []
        for res in results:
            words = res.get("words")
            if not isinstance(words, list) or len(words) > MAX_IMPORT_WORDS:
                raise ValueError("a result's words are missing or too many")
            try:
                words = [[round(float(w[0]), 2), round(float(w[1]), 2), str(w[2])[:80]] for w in words]
            except (TypeError, ValueError, IndexError):
                raise ValueError("a result's words aren't [start, end, word]") from None
            model = str(res.get("model") or "?")[:64]
            tmp = self.dir / f"{item_id}.import.json.gz"
            self._write(tmp, {"words": words})
            try:
                result, match_took = self._match(item_id, lines, tmp)
            finally:
                tmp.unlink(missing_ok=True)
            aligned = self._aligned(result, lines)

            def num(key, cast=float):
                try:
                    return cast(res[key]) if res.get(key) is not None else None
                except (TypeError, ValueError):
                    return None
            threads, workers = num("threads", int), num("workers", int)
            timing = {"source": "pc", "host": str(res.get("host") or "")[:80] or None,
                      "device": str(res.get("device") or "")[:40] or None,
                      "compute_type": str(res.get("compute_type") or "")[:20] or None,
                      "program": str(res.get("program") or "")[:20] or None,
                      "timeline": sound["timeline"], "sound_post": sound["post_id"],
                      "duration": round(float(res.get("until") or sound["duration"] or 0), 1) or None,
                      "sound_size": sound["size"], "model": model, "threads": threads, "workers": workers,
                      "threads_used": [threads] if threads else [], "batch": num("batch", int),
                      "took": num("took"), "load": num("load"), "decode": num("decode"), "peak_mb": num("peak_mb", int),
                      "started": None, "sessions": 1, "match_took": match_took, "heard_now": True,
                      "heard": len(words), "words": result.get("words"), "found": result["found"],
                      "placed": result["placed"], "unplaced": result["unplaced"], "at": _now(),
                      "error": None if aligned else self._mismatch(result)}
            tries.append({"timing": timing, "result": result, "aligned": aligned, "words": words})
        if len(tries) > 1:
            base = tries[0]
            for other in tries[1:]:
                c = compare_lines(base["result"]["lines"], other["result"]["lines"])
                other["timing"]["compare"] = dict(c, model=base["timing"]["model"])
                base["timing"].setdefault("compare", dict(c, model=other["timing"]["model"]))
        # The timing in use now, if it was made from this same sound: each try is compared with it too, and it stays
        # in use when it found more of the transcript's words than the best of these (v0.25)
        row = self.row(item_id) or {}
        current = row.get("timing") or {}
        if not (current.get("at") and current.get("found") is not None and current.get("sound_size") == sound["size"]):
            current = {}
        before = None
        if current:
            before = {"model": current.get("model"), "source": current.get("source") or "server",
                      "found": current["found"], "at": current.get("at")}
            if row.get("aligned") and stored.get("aligned"):
                for x in tries:
                    c = compare_lines(stored.get("lines") or [], x["result"]["lines"])
                    x["timing"]["compare_in_use"] = dict(c, model=current.get("model"), source=before["source"])
        best = max(tries, key=lambda x: (x["aligned"], x["result"]["found"]))
        replaced = not current or (best["aligned"], best["result"]["found"]) >= (bool(row.get("aligned")),
                                                                               current["found"])
        if replaced:
            best["timing"]["used"] = True
            self._store(primary, stored, lines, sound, best["result"], best["aligned"], best["timing"])
            # What was heard, as if heard here, so Time it again only matches again (with the same model)
            self._write(self.heard_file(item_id), {
                "key": {"path": sound["path"], "size": sound["size"], "model": best["timing"]["model"]},
                "until": best["timing"]["duration"] or 0.0, "words": best["words"],
                "took": best["timing"]["took"] or 0.0, "peak_mb": best["timing"]["peak_mb"] or 0,
                "load": best["timing"]["load"], "sessions": 1, "threads_used": best["timing"]["threads_used"]})
        for x in tries:
            self.record(primary, x["timing"])
        return {"item_id": item_id, "used": best["timing"]["model"] if replaced else (before["model"] or "?"),
                "replaced": replaced, "before": before,
                "tries": [{k: x["timing"].get(k) for k in ("model", "device", "found", "placed", "unplaced", "took",
                                                            "load", "duration", "peak_mb", "error", "compare",
                                                            "compare_in_use", "used")}
                          | {"speed": record_figures(x["timing"])["speed"]} for x in tries]}

    def record(self, primary: dict, timing: dict) -> None:
        """Keep this try in the record of every timing (Admin > Transcripts), beside the episode's latest."""
        self.db.x("INSERT INTO transcript_runs (item_id, title, at, timing) VALUES (?, ?, ?, ?)",
                  (primary["id"], primary.get("title"), timing.get("at"), json.dumps(timing)))

    def runs(self, item_id: int | None = None) -> list[dict]:
        if item_id is None:
            return self.db.q("SELECT * FROM transcript_runs ORDER BY at DESC, id DESC")
        return self.db.q("SELECT * FROM transcript_runs WHERE item_id=? ORDER BY at DESC, id DESC", (item_id,))

    # what needs doing
    def to_read(self, found: list) -> list:
        out = []
        for primary, members, doc in found:
            row = self.row(primary["id"])
            stored = self._load(self.lines_file(primary["id"])) if row else None
            if not row or row["document"] != doc["path"] or row["doc_size"] != doc["size"] or not stored \
                    or stored.get("version") != VERSION:
                out.append((primary, members, doc))
        return out

    def to_time(self, found: list) -> list:
        out = []
        for primary, members, doc in found:
            row = self.row(primary["id"])
            sound = self.sound(members)
            if not row or not sound:
                continue
            timing = row["timing"] or {}
            if timing.get("at") and timing.get("sound_size", sound["size"]) == sound["size"]:
                continue   # done, or tried: a mismatch stays as it is until the transcript or the sound changes
            out.append((primary, members, doc))
        return out

    def newest_untimed(self, found: list) -> int | None:
        """The newest item with a transcript that has sound to hear and hasn't been timed or tried, read or not."""
        for primary, members, doc in found:
            row = self.row(primary["id"])
            if self.sound(members) and (not row or (not row["aligned"] and not (row["timing"] or {}).get("at"))):
                return primary["id"]
        return None

    def forget_gone(self, found: list) -> None:
        keep = {p["id"] for p, _, _ in found}
        for row in self.db.q("SELECT item_id FROM transcripts"):
            if row["item_id"] not in keep:
                self.db.x("DELETE FROM transcripts WHERE item_id=?", (row["item_id"],))
                self.lines_file(row["item_id"]).unlink(missing_ok=True)
                self.heard_file(row["item_id"]).unlink(missing_ok=True)

    def run(self, ser, should_yield, one: int | None = None, read_on: bool = True, timing_on: bool = False) -> dict:
        """Read new and changed transcripts, then time them (newest first) when timing is on, or the one asked for.
        Returns what was done; raises Yielded when something else needs the scheduler."""
        found = self.items(ser)
        self.forget_gone(found)
        done = {"read": 0, "timed": 0}
        pending = self.to_read(found) if read_on else []
        if one is not None:
            pending = [x for x in pending if x[0]["id"] == one]
        if pending:
            self.status.begin("transcripts", total=len(pending), phase="Reading transcripts")
            for i, (primary, members, doc) in enumerate(pending):
                self.status.set(done=i, detail=primary["title"] or str(primary["id"]))
                try:
                    self.read(primary, doc, should_yield)
                    done["read"] += 1
                except Yielded:
                    raise
                except Exception as e:
                    log.warning("reading the transcript of %s failed: %s", primary["id"], e)
        if one is not None:
            work = [x for x in found if x[0]["id"] == one]
        else:
            work = self.to_time(found) if timing_on else []
        if work:
            self.status.begin("transcripts", total=len(work), phase="Timing transcripts")
            for i, (primary, members, doc) in enumerate(work):
                title = primary["title"] or str(primary["id"])

                pace = Pace()

                def progress(share: float, what: str, i=i, title=title, pace=pace) -> None:
                    share = min(1.0, share)
                    detail = f"{title} · {what}, {round(share * 100)}%"
                    if what == "hearing":
                        left = pace.left(share)
                        if left is not None:
                            detail += f", {left_text(left)}"
                    else:
                        pace.reset()   # loading the model, or matching: not hearing
                    self.status.set(done=i, current=round(share, 3), detail=detail)
                try:
                    self.time(primary, members, should_yield, progress)
                    done["timed"] += 1
                    self.db.set_setting("transcripts_problem", "")
                except (Yielded, ModelProblem):
                    raise
                except SoundChanged:
                    log.info("the sound of %s changed while it was heard; it's heard again next time", primary["id"])
                except Exception as e:
                    log.warning("timing the transcript of %s failed: %s", primary["id"], e)
                    self.record(primary, {"error": str(e)[:300], "at": _now(), "model": self.model(),
                                          "threads": self.threads()})
                    self.db.update("transcripts", "item_id", primary["id"], {
                        "timing": {"error": str(e)[:300], "at": _now()}, "updated_at": _now()})
        return done

    # for the API
    def lines_for(self, item_id: int, timeline: str | None, offset: float) -> dict | None:
        """The lines, with times moved to the timeline asked for (audio = video + offset, as sync says)."""
        stored = self._load(self.lines_file(item_id))
        if not stored:
            return None
        shift = 0.0
        if stored.get("aligned") and timeline and stored.get("timeline") and timeline != stored["timeline"]:
            shift = -offset if stored["timeline"] == "audio" else offset
        lines = []
        for l in stored["lines"]:
            s, e = l.get("start"), l.get("end")
            if s is not None and stored.get("aligned"):
                s, e = round(max(0.0, s + shift), 2), round(max(0.0, (e if e is not None else s) + shift), 2)
            else:
                s = e = None
            lines.append({"start": s, "end": e, "speaker": l.get("speaker"), "text": l.get("text")})
        return {"aligned": bool(stored.get("aligned")), "lines": lines, "document": (stored.get("document") or {}).get("path")}

    def summary(self) -> dict:
        rows = self.db.q("SELECT * FROM transcripts")
        timed = [r for r in rows if (r["timing"] or {}).get("at") and (r["timing"] or {}).get("took") is not None]
        last = max(timed, key=lambda r: r["timing"]["at"], default=None)
        return {"count": len(rows), "aligned": sum(1 for r in rows if r["aligned"]),
                "problem": self.db.setting("transcripts_problem") or None,
                "failed": sum(1 for r in rows if (r["timing"] or {}).get("error")),
                "last": dict(last["timing"], item_id=last["item_id"]) if last else None}


def compare_lines(a: list[dict], b: list[dict]) -> dict:
    """How far apart two timings of the same lines put them: of the lines both placed, how many seconds their
    starts differ (the median, and nine in ten within), and how many each placed that the other didn't."""
    diffs, only_a, only_b = [], 0, 0
    for x, y in zip(a, b):
        sx, sy = x.get("start"), y.get("start")
        if sx is not None and sy is not None:
            diffs.append(abs(sx - sy))
        elif sx is not None:
            only_a += 1
        elif sy is not None:
            only_b += 1
    diffs.sort()

    def at(q):
        return round(diffs[min(len(diffs) - 1, int(q * len(diffs)))], 2) if diffs else None
    return {"both": len(diffs), "median": at(0.5), "p90": at(0.9),
            "within_1s": round(sum(1 for d in diffs if d <= 1.0) / len(diffs), 3) if diffs else None,
            "only_this": only_a, "only_other": only_b}


def record_figures(timing: dict | None) -> dict:
    """A try's figures as the records page shows them: what came of it, how fast it heard (the episode's length over
    the time hearing took), and the share of lines placed."""
    tm = dict(timing or {})
    took, duration, placed, unplaced = tm.get("took"), tm.get("duration"), tm.get("placed"), tm.get("unplaced")
    if not tm.get("at"):
        result = "waiting"
    elif tm.get("error"):
        result = "mismatch" if tm.get("found") is not None else "error"
    else:
        result = "timed"
    lines = (placed or 0) + (unplaced or 0)
    used = tm.get("threads_used") or ([tm["threads"]] if tm.get("threads") else [])
    text = (", ".join(str(n) for n in used) + (" thread" if used == [1] else " threads")) if used else ""
    if tm.get("device") and str(tm["device"]).startswith("cuda"):
        text = "GPU" + (f", batches of {tm['batch']}" if tm.get("batch") else "")
    elif tm.get("workers") and tm["workers"] > 1 and used:
        text = f"{tm['workers']} × {used[-1]} threads"
    return dict(tm, result=result, speed=round(duration / took, 2) if took and duration else None,
                placed_share=(placed / lines) if placed is not None and lines else None,
                threads_text=text, where="PC" if tm.get("source") == "pc" else "server")


def _same_file(sound: dict) -> bool:
    """Whether the sound being heard is still there as it was when hearing began."""
    try:
        return os.stat(sound["path"]).st_size == sound["size"]
    except OSError:
        return False


def _now() -> str:
    from .db import now
    return now()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
