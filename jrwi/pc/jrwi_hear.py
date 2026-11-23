#!/usr/bin/env python3
"""Hear JRWI episodes on this computer for the JRWI archiver, and send back what was heard.

The archiver times an episode's transcript by hearing the episode with a speech model (faster-whisper) and matching
the transcript's words to the words it heard. Hearing is the slow part, so this program does it on a faster computer
and sends the words back. The archiver still does the matching, so the lines come out exactly as if it had heard the
episode itself, and every try shows up in its Admin > Transcripts record, marked PC.

Install once (Python 3.9 or newer):
    pip install faster-whisper psutil
For an NVIDIA graphics card, which hears many times faster:
    pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
(If the card still isn't used on Windows, unpack the NVIDIA libraries from Purfview's whisper-standalone-win into a
folder named nvidia next to this program, or run with --device cpu.)

Use:
    python jrwi_hear.py setup              the archiver's address and API token (its Settings page shows the token)
    python jrwi_hear.py check              whether it reaches the archiver and can use the graphics card
    python jrwi_hear.py list [--all]       episodes not timed yet; with --all every episode, with what has heard it
    python jrwi_hear.py compare [POST ...] hear episodes once per model (medium.en, large-v3-turbo and large-v3), send
                                           them and compare: the archiver keeps whichever found the most words
    python jrwi_hear.py run [--all]        hear episodes not timed yet (medium.en), newest first, sending each as it's
                                           done; with --all every episode, skipping those the model has heard
                                           (--again: those too), so running it again carries on where it stopped
    python jrwi_hear.py file JOB.zip       without the network: an "Export for your PC" file from an episode's
                                           admin page; writes a result file to import on the same page

Options: --models medium.en,large-v3   --device auto|cuda|cpu   --batch 16 (graphics card)   --count N
         --workers 4 --threads 6 (processor: pieces heard at once, and threads for each)   --keep (keep the sound)
POST is the number at the end of an episode's address in the archiver (/items/123).
The archiver keeps whichever timing found more of the transcript's words, the one in use or a new one.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PROGRAM = "1.1"
HERE = Path(__file__).resolve().parent
CONFIG = HERE / "jrwi_hear.json"
CACHE = HERE / "jrwi_hear_cache"
MODELS = HERE / "jrwi_hear_models"


# ------------------------------------------------------------------ small things

def say(*parts) -> None:
    print(*parts, flush=True)


def die(msg: str) -> None:
    say(f"\n{msg}")
    sys.exit(1)


def clock(seconds) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def pct(x) -> str:
    return "-" if x is None else f"{round(x * 100)}%"


def pct1(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def load_config(args) -> dict:
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
        except ValueError:
            pass
    cfg["server"] = (getattr(args, "server", None) or os.environ.get("JRWI_SERVER") or cfg.get("server") or "").rstrip("/")
    cfg["token"] = os.environ.get("JRWI_TOKEN") or cfg.get("token") or ""
    return cfg


def api(cfg: dict, method: str, path: str, body=None, timeout: float = 120, soft: bool = False):
    """The archiver's answer. soft: an episode it can't take (gone, or its sound changed) is skipped (None)."""
    if not cfg.get("server") or not cfg.get("token"):
        die("The archiver's address and token aren't set up yet: run  python jrwi_hear.py setup")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(cfg["server"] + path, data=data, method=method,
                                 headers={"Authorization": f"Token {cfg['token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except ValueError:
            msg = e.reason
        if e.code == 401:
            msg = "the token wasn't accepted: run  python jrwi_hear.py setup  again"
        elif e.code == 404 and "/api/transcripts/" in path and "jobs" in path and msg in ("Not Found", None):
            msg = "the archiver doesn't know this yet: update it to 0.14.1 or newer"
        elif soft and e.code in (404, 409):
            say(f"  Skipped: {msg}")
            return None
        die(f"The archiver said: {msg}")
    except urllib.error.URLError as e:
        die(f"Couldn't reach the archiver at {cfg['server']}: {e.reason}")


def download(cfg: dict, job: dict) -> Path:
    """The episode's sound, kept in jrwi_hear_cache until it's been sent (or for good with --keep)."""
    s = job["sound"]
    CACHE.mkdir(exist_ok=True)
    dest = CACHE / f"{s['post_id']}-{s['size']}{Path(s.get('name') or '').suffix or '.m4a'}"
    if dest.exists() and dest.stat().st_size == s["size"]:
        return dest
    req = urllib.request.Request(cfg["server"] + s["url"], headers={"Authorization": f"Token {cfg['token']}"})
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
            got, last = 0, 0.0
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
                got += len(block)
                if time.monotonic() - last > 0.5:
                    print(f"\r  Downloading the sound: {got >> 20} of {s['size'] >> 20} MB", end="", flush=True)
                    last = time.monotonic()
    except urllib.error.URLError as e:
        die(f"Couldn't download the sound: {getattr(e, 'reason', e)}")
    print(f"\r  Downloaded the sound: {s['size'] >> 20} MB" + " " * 20, flush=True)
    if part.stat().st_size != s["size"]:
        part.unlink(missing_ok=True)
        die("The sound didn't download whole; try again.")
    part.replace(dest)
    return dest


# ------------------------------------------------------------------ the graphics card

def nvidia_dirs() -> list[str]:
    """Where NVIDIA's libraries are: a folder named nvidia next to this program (where Purfview's archive can be
    unpacked), and where pip put nvidia-cublas-cu12 and nvidia-cudnn-cu12, if it did."""
    out = [str(HERE / "nvidia")] if (HERE / "nvidia").is_dir() else []
    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        return out
    for base in (spec.submodule_search_locations if spec else None) or []:
        for lib in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            d = Path(base) / lib / ("bin" if os.name == "nt" else "lib")
            if d.is_dir():
                out.append(str(d))
    return out


def use_nvidia_libraries() -> None:
    """Make pip's NVIDIA libraries findable. Windows looks on PATH; Linux reads LD_LIBRARY_PATH only as a program
    starts, so this starts the program again with it set."""
    dirs = nvidia_dirs()
    if not dirs:
        return
    if os.name == "nt":
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        for d in dirs:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
    elif not os.environ.get("JRWI_HEAR_LIBS"):
        env = dict(os.environ, JRWI_HEAR_LIBS="1",
                   LD_LIBRARY_PATH=os.pathsep.join(dirs + [os.environ.get("LD_LIBRARY_PATH", "")]))
        os.execve(sys.executable, [sys.executable, *sys.argv], env)


def pick_device(want: str) -> str:
    if want == "cpu" or os.environ.get("JRWI_FAKE_HEARING"):
        return "cpu"
    try:
        import ctranslate2
        cards = ctranslate2.get_cuda_device_count()
    except Exception:
        cards = 0
    if cards > 0:
        return "cuda"
    if want == "cuda":
        die("No NVIDIA graphics card could be used. Install its libraries (see the top of this file), or use "
            "--device cpu.")
    return "cpu"


def cores() -> tuple[int, int]:
    logical = os.cpu_count() or 8
    try:
        import psutil
        physical = psutil.cpu_count(logical=False) or logical // 2
    except ImportError:
        physical = max(1, logical // 2)
    return physical, logical


def peak_mb() -> int | None:
    try:
        import psutil
        peak = getattr(psutil.Process().memory_info(), "peak_wset", 0)   # Windows keeps the peak
        if peak:
            return int(peak / 1048576)
    except ImportError:
        pass
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)   # Linux: KB
    except ImportError:
        return None


# ------------------------------------------------------------------ hearing (in a process of its own per model)

def decode_sound(path: Path):
    """The sound as 16 kHz mono samples, as faster-whisper hears it. faster-whisper's own decode_audio passes PyAV
    an option that newer PyAV versions no longer take, so this decodes the same way without it."""
    import av
    import numpy as np
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    parts = []
    with av.open(str(path), mode="r") as container:
        frames = container.decode(audio=0)
        while True:
            try:
                frame = next(frames)
            except StopIteration:
                break
            except av.error.InvalidDataError:   # a damaged frame: skip it, as faster-whisper does
                continue
            frame.pts = None
            parts.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
        parts.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32) / 32768.0


def hear(job: dict, audio: Path, model_name: str, opts: dict) -> dict:
    """Hear the episode as the archiver would (the job says how), in its pieces, several at once."""
    h = job["hearing"]
    chunk, overlap = float(h["chunk"]), float(h["overlap"])
    device = opts["device"]
    fake = os.environ.get("JRWI_FAKE_HEARING")   # tests: the words come from a file
    began = time.monotonic()
    if fake:
        words = json.loads(Path(fake).read_text())
        if "small" in model_name:   # a smaller model mishears more
            words = [w for i, w in enumerate(words) if i % 4 != 3]
        total = float(job["sound"].get("duration") or 0)
        return {"model": model_name, "device": device, "words": words, "until": total, "took": 0.5, "load": 0.1,
                "decode": 0.0, "peak_mb": 100, "threads": opts["threads"], "workers": opts["workers"],
                "batch": None, "host": platform.node(), "program": PROGRAM, "compute_type": "test"}
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    say(f"  {model_name}: loading the model (downloaded once, the first time)")
    compute = "float16" if device == "cuda" else "int8"
    model = WhisperModel(model_name, device=device, compute_type=compute,
                         cpu_threads=opts["threads"] if device == "cpu" else 0,
                         num_workers=opts["workers"] if device == "cpu" else 1, download_root=str(MODELS))
    load = time.monotonic() - began
    started = time.monotonic()
    audio_data = decode_sound(audio)
    decode = time.monotonic() - started
    total = len(audio_data) / 16000.0
    pipe = BatchedInferencePipeline(model=model) if device == "cuda" and opts["batch"] > 1 else None
    starts, t = [], 0.0
    while t < total - 0.5:
        starts.append(t)
        t += chunk
    kw = dict(language=h["language"], beam_size=h["beam_size"], word_timestamps=True, vad_filter=h["vad_filter"],
              condition_on_previous_text=h["condition_on_previous_text"], hotwords=job.get("hotwords") or None)

    def piece(t: float) -> list:
        length = min(chunk + overlap, total - t)
        lo = t + overlap / 2 if t > 0 else 0.0
        hi = t + chunk + overlap / 2 if t + chunk < total else float("inf")
        sound = audio_data[int(t * 16000): int((t + length) * 16000)]
        segments, _ = (pipe.transcribe(sound, batch_size=opts["batch"], **kw) if pipe else model.transcribe(sound, **kw))
        out = []
        for seg in segments:
            for w in seg.words or []:
                s = t + w.start
                if lo <= s < hi and w.word.strip():
                    out.append([round(s, 2), round(t + w.end, 2), w.word.strip()])
        return out

    from concurrent.futures import ThreadPoolExecutor, as_completed
    words, done = [], 0
    with ThreadPoolExecutor(opts["workers"] if device == "cpu" else 1) as pool:
        futures = [pool.submit(piece, t) for t in starts]
        for f in as_completed(futures):
            words.extend(f.result())
            done += 1
            spent = time.monotonic() - started
            left = spent / done * (len(starts) - done)
            print(f"\r  {model_name}: {done} of {len(starts)} pieces heard, {clock(spent)} so far, "
                  f"about {clock(left)} left   ", end="", flush=True)
    took = time.monotonic() - started   # decoding and hearing, not loading the model
    say(f"\r  {model_name}: heard {clock(total)} of sound in {clock(took)} ({total / took:.1f}x as fast as it plays)"
        + " " * 20)
    words.sort(key=lambda w: (w[0], w[1]))
    return {"model": model_name, "device": device, "compute_type": compute, "words": words, "until": round(total, 2),
            "took": round(took, 1), "load": round(load, 1), "decode": round(decode, 1), "peak_mb": peak_mb(),
            "threads": opts["threads"] if device == "cpu" else None, "workers": opts["workers"] if device == "cpu" else 1,
            "batch": opts["batch"] if pipe else None, "host": platform.node(), "program": PROGRAM}


def hear_in_own_process(job: dict, audio: Path, model: str, opts: dict) -> dict:
    """Each model in a process of its own, so its memory (and the card's) is free again for the next, and the
    memory figure is that model's alone."""
    with tempfile.TemporaryDirectory() as tmp:
        job_file, out_file = Path(tmp) / "job.json", Path(tmp) / "out.json"
        job_file.write_text(json.dumps(job))
        cmd = [sys.executable, str(Path(__file__).resolve()), "_hear", str(job_file), str(audio), model, str(out_file),
               "--device", opts["device"], "--workers", str(opts["workers"]), "--threads", str(opts["threads"]),
               "--batch", str(opts["batch"])]
        code = subprocess.call(cmd)
        if code != 0 or not out_file.exists():
            hint = (" If the message above mentions cudnn, cublas or CUDA, the graphics card's libraries are missing: "
                    "see the top of jrwi_hear.py, or use --device cpu.") if opts["device"] == "cuda" else ""
            die(f"Hearing with {model} stopped (exit code {code}).{hint}")
        return json.loads(out_file.read_text())


# ------------------------------------------------------------------ commands

DEFAULT_COMPARE = "medium.en,large-v3-turbo,large-v3"
DEFAULT_RUN = "medium.en"


def model_list(text: str) -> list[str]:
    models = list(dict.fromkeys(m.strip() for m in text.split(",") if m.strip()))
    if not models:
        die("Name at least one model with --models, such as  --models medium.en")
    if len(models) > 6:
        die("At most six models at a time.")
    return models


def stay_awake() -> None:
    """While this runs, Windows doesn't put the computer to sleep (the screen can still turn off)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)   # continuous, system required
        except Exception:
            pass


def options(args) -> dict:
    device = pick_device(args.device)
    physical, _ = cores()
    workers = args.workers or (4 if physical >= 12 else 2 if physical >= 6 else 1)
    threads = args.threads or max(1, physical // workers)
    return {"device": device, "workers": workers, "threads": threads, "batch": args.batch}


def describe(opts: dict, models: list[str]) -> None:
    how = (f"the graphics card, in batches of {opts['batch']}" if opts["device"] == "cuda" else
           f"the processor, {opts['workers']} pieces at once with {opts['threads']} threads each")
    say(f"Hearing with {', '.join(models)} on {how}.")


def where(source) -> str:
    return "a PC" if source == "pc" else "the server"


def archiver(cfg: dict) -> dict:
    ping = api(cfg, "GET", "/api/ping")
    if "transcript_jobs" not in (ping.get("features") or []):
        die(f"The archiver ({ping.get('version')}) can't hand out episodes yet: update it to 0.14.1 or newer.")
    return ping


def has_all(ping: dict) -> bool:
    try:
        return tuple(int(x) for x in str(ping.get("contract") or "0").split(".")) >= (0, 25)
    except ValueError:
        return False


def all_jobs(cfg: dict, ping: dict | None = None) -> dict:
    """Every episode the archiver can hand out, timed or not, newest first, and those it can't, with why."""
    ping = ping or archiver(cfg)
    if not has_all(ping):
        die(f"The archiver ({ping.get('version')}) only hands out episodes that aren't timed yet: update it to "
            "0.14.1 or newer.")
    return api(cfg, "GET", "/api/transcripts/jobs?all=true", timeout=600)


def show_left_out(out: dict) -> None:
    for x in out.get("left_out") or []:
        say(f"  Left out: {x['title']} ({x['item_id']}): {x['why']}")


def send(cfg: dict, job: dict, results: list[dict], soft: bool = False):
    say("  Sending what was heard; the archiver matches it to the transcript...")
    return api(cfg, "POST", f"/api/transcripts/jobs/{job['item_id']}",
               {"item_id": job["item_id"], "sound_size": job["sound"]["size"], "results": results}, timeout=900,
               soft=soft)


def lines_text(t: dict) -> str:
    if t.get("placed") is None:
        return "-"
    return f"{t['placed']} of {(t.get('placed') or 0) + (t.get('unplaced') or 0)}"


def show_results(out: dict) -> None:
    say("")
    say(f"  {'Model':<16} {'Words found':>11} {'Lines placed':>14} {'Hearing':>9} {'Speed':>7} {'Memory':>8}")
    for t in out["tries"]:
        mem = f"{t['peak_mb']} MB" if t.get("peak_mb") else "-"
        speed = f"{t['speed']}x" if t.get("speed") else "-"
        mark = "  <- in use" if t.get("used") else ""
        say(f"  {t['model']:<16} {pct1(t.get('found')):>11} {lines_text(t):>14} {clock(t.get('took')):>9} "
            f"{speed:>7} {mem:>8}{mark}")
        if t.get("error"):
            say(f"    {t['error']}")
    for t in out["tries"][1:]:
        c = t.get("compare")
        if c and c.get("both"):
            say(f"\n  {t['model']} against {c['model']}: of the {c['both']} lines both placed, half start within "
                f"{c['median']} s of each other, nine in ten within {c['p90']} s ({pct(c['within_1s'])} within a "
                f"second). {t['model']} placed {c['only_this']} lines the other didn't; {c['model']} placed "
                f"{c['only_other']}.")
    for t in out["tries"]:
        c = t.get("compare_in_use")
        if c and c.get("both"):
            say(f"\n  {t['model']} against the timing that was in use ({c['model']}, from {where(c.get('source'))}): "
                f"half the lines start within {c['median']} s of where it had them, nine in ten within {c['p90']} s.")
    say("")
    say(in_use_text(out))


def in_use_text(out: dict) -> str:
    before = out.get("before")
    used = next((t for t in out["tries"] if t.get("used")), None)
    if not out.get("replaced", True) and before:
        return (f"  Kept the timing in use: {before['model']} from {where(before['source'])} found "
                f"{pct1(before['found'])} of the transcript's words, more than this.")
    text = f"  In use now: {out['used']}" + (f", {pct1(used.get('found'))} of words found" if used else "")
    if before:
        text += f", in place of {before['model']} from {where(before['source'])} ({pct1(before['found'])})"
    return text + "."


def show_summary(summary: dict) -> None:
    say("\nAll the episodes together:")
    say(f"  {'Model':<16} {'Episodes':>8} {'Words found':>11} {'Lines placed':>12} {'Hearing':>9} {'Speed':>7}")
    for model, tries in summary.items():
        ok = [t for t in tries if t.get("found") is not None]
        if not ok:
            continue
        found = sum(t["found"] for t in ok) / len(ok)
        placed = sum(t.get("placed") or 0 for t in ok)
        lines = sum((t.get("placed") or 0) + (t.get("unplaced") or 0) for t in ok)
        took = sum(t.get("took") or 0 for t in ok)
        length = sum(t.get("length") or 0 for t in ok)
        speed = f"{length / took:.0f}x" if took else "-"
        say(f"  {model:<16} {len(ok):>8} {pct1(found):>11} {pct1(placed / lines) if lines else '-':>12} "
            f"{clock(took):>9} {speed:>7}")
    say("  Words found is the average over the episodes, lines placed is of all their lines together, and hearing "
        "is the total time.")


def cmd_setup(args) -> None:
    cfg = load_config(args)
    default = cfg.get("server") or "http://box:8090"
    server = input(f"The archiver's address [{default}]: ").strip() or default
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    token = getpass.getpass("Its API token (Settings in the archiver's web app; it isn't shown as you type): ").strip()
    cfg = {"server": server.rstrip("/"), "token": token or cfg.get("token", "")}
    ping = archiver(cfg)
    CONFIG.write_text(json.dumps(cfg, indent=1))
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass
    say(f"Connected to the archiver {ping.get('version')}. Saved to {CONFIG.name}, which holds the token: keep it to "
        "yourself.")
    say("Next:  python jrwi_hear.py check")


def gpu_name() -> str | None:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=20)
        line = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
        return line.replace(", ", " with ", 1) if line else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def cmd_check(args) -> None:
    cfg = load_config(args)
    ping = archiver(cfg)
    say(f"Archiver: {cfg['server']}, version {ping.get('version')}.")
    if has_all(ping):
        out = all_jobs(cfg, ping)
        jobs = out["jobs"]
        untimed = sum(1 for j in jobs if not j.get("timed_with"))
        hours = sum(j["sound"].get("duration") or 0 for j in jobs) / 3600
        say(f"Episodes: {out['episodes']} with a transcript. {len(jobs)} can be heard ({hours:.0f} hours of sound), "
            f"{untimed} of them not timed yet.")
        show_left_out(out)
    else:
        say("This archiver only hands out episodes that aren't timed yet: update it to 0.14.1 or newer for --all.")
    physical, logical = cores()
    say(f"Processor: {physical} cores, {logical} threads.")
    if args.device == "cpu" or os.environ.get("JRWI_FAKE_HEARING"):
        say("Graphics card: not checked.")
        return
    gpu = gpu_name()
    try:
        import ctranslate2
        cards = ctranslate2.get_cuda_device_count()
    except Exception as e:
        say(f"faster-whisper isn't installed properly ({e}): run  pip install faster-whisper psutil")
        return
    if not cards:
        say("Graphics card: none that faster-whisper can use" + (f" (the driver sees {gpu})" if gpu else "")
            + ", so hearing would use the processor. Update the NVIDIA driver; see the guide's last section.")
        return
    say(f"Graphics card: {gpu or 'an NVIDIA card'}. Trying it with the tiny model (75 MB, downloaded once)...")
    code = subprocess.call([sys.executable, str(Path(__file__).resolve()), "_probe"])
    if code == 0:
        say("The graphics card works: hearing will use it.")
    else:
        say("The graphics card couldn't run the model: its cuBLAS or cuDNN libraries are missing or the wrong "
            "version (the message above says which). See the guide's last section, or use --device cpu.")


def cmd_probe(args) -> None:   # check's test of the graphics card, in a process of its own
    use_nvidia_libraries()
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel("tiny.en", device="cuda", compute_type="float16", download_root=str(MODELS))
    segments, _ = model.transcribe(np.zeros(32000, dtype=np.float32), language="en", beam_size=1,
                                   word_timestamps=True)
    list(segments)


def cmd_list(args) -> None:
    cfg = load_config(args)
    out = all_jobs(cfg) if args.all else api(cfg, "GET", "/api/transcripts/jobs?limit=5000")
    jobs = out["jobs"]
    if not jobs:
        say("No episode can be heard yet." if args.all else "Every episode with a transcript is timed.")
    else:
        say(f"  {'Post':>8}  {'Length':>8}  {'Lines':>5}  {'In use':<28}  {'Heard with':<34}  Title")
    for j in jobs:
        in_use = (f"{j['timed_with']} {pct1(j.get('found'))} ({'PC' if j.get('source') == 'pc' else 'server'})"
                  if j.get("timed_with") else "not timed")
        heard = ", ".join(j.get("heard_with") or []) or "-"
        say(f"  {j['item_id']:>8}  {clock(j['sound'].get('duration')):>8}  {j['lines']:>5}  {in_use:<28}  "
            f"{heard:<34}  {j['title']}")
    show_left_out(out)


def cmd_compare(args) -> None:
    cfg = load_config(args)
    models = model_list(args.models)
    opts = options(args)
    if opts["device"] == "cuda":
        use_nvidia_libraries()
    stay_awake()
    if args.posts:
        jobs = [api(cfg, "GET", f"/api/transcripts/jobs/{p}") for p in args.posts]
    else:
        ping = archiver(cfg)
        jobs = (all_jobs(cfg, ping) if has_all(ping) else api(cfg, "GET", "/api/transcripts/jobs?limit=1"))["jobs"][:1]
        if not jobs:
            die("No episode can be heard yet. Give post numbers:  compare 123 456")
    describe(opts, models)
    summary: dict = {m: [] for m in models}
    for n, job in enumerate(jobs, 1):
        say(f"\n[{n} of {len(jobs)}] {job['title']} ({clock(job['sound'].get('duration'))}, {job['lines']} lines)")
        audio = download(cfg, job)
        results = [hear_in_own_process(job, audio, m, opts) for m in models]
        out = send(cfg, job, results)
        show_results(out)
        for t in out["tries"]:
            summary.setdefault(t["model"], []).append(dict(t, length=job["sound"].get("duration")))
        if not args.keep:
            audio.unlink(missing_ok=True)
    if len(jobs) > 1:
        show_summary(summary)
    say("\nEach episode's admin page, and Admin > Transcripts, show the same in the archiver.")


def cmd_run(args) -> None:
    cfg = load_config(args)
    models = model_list(args.models)
    opts = options(args)
    if opts["device"] == "cuda":
        use_nvidia_libraries()
    stay_awake()
    describe(opts, models)
    if args.all:
        out = all_jobs(cfg)
        todo = []
        for j in out["jobs"]:
            need = models if args.again else [m for m in models if m not in (j.get("heard_with") or [])]
            if need:
                todo.append(j)
        skipped = len(out["jobs"]) - len(todo)
        say(f"{len(out['jobs'])} episodes can be heard: {len(todo)} to do"
            + (f", {skipped} already heard with {', '.join(models)}" if skipped else "") + ".")
        show_left_out(out)
        if args.count:
            todo = todo[:args.count]
        queue = list(todo)
    else:
        queue = None
    done, kept, heard_s, began = 0, 0, 0.0, time.monotonic()
    while not args.count or done < args.count:
        if queue is not None:
            if not queue:
                break
            # Asked again just before hearing it: the sound, and what has heard it, as they are now
            job = api(cfg, "GET", f"/api/transcripts/jobs/{queue.pop(0)['item_id']}", soft=True)
            if job is None:
                continue
            need = models if args.again else [m for m in models if m not in (job.get("heard_with") or [])]
            if not need:
                continue
            left = len(queue)
        else:
            jobs = api(cfg, "GET", "/api/transcripts/jobs?limit=1")["jobs"]
            if not jobs:
                say("\nEvery episode with a transcript is timed.")
                break
            job, need, left = jobs[0], models, None
        count = f"[{done + 1} of {done + 1 + left}] " if left is not None else ""
        say(f"\n{count}{job['title']} ({clock(job['sound'].get('duration'))}, {job['lines']} lines)")
        audio = download(cfg, job)
        results = [hear_in_own_process(job, audio, m, opts) for m in need]
        out = send(cfg, job, results, soft=True)
        if not args.keep:
            audio.unlink(missing_ok=True)
        if out is None:
            continue
        for t in out["tries"]:
            say(f"  {t['model']}: {pct1(t.get('found'))} of words found, {lines_text(t)} lines placed"
                + (f". {t['error']}" if t.get("error") else ""))
        say(in_use_text(out))
        kept += 0 if out.get("replaced", True) else 1
        done += 1
        heard_s += job["sound"].get("duration") or 0
        if queue:
            spent = time.monotonic() - began
            rest = sum(j["sound"].get("duration") or 0 for j in queue)
            if heard_s:
                say(f"  {clock(spent)} so far; about {clock(spent / heard_s * rest)} left for the other {len(queue)}.")
    say(f"\n{done} episode(s) heard in {clock(time.monotonic() - began)}."
        + (f" {kept} kept the timing that was in use, which found more words." if kept else ""))
    if queue is not None:
        say("Running the same command again carries on with what's left, and skips what's done.")


def cmd_file(args) -> None:
    models = model_list(args.models)
    opts = options(args)
    if opts["device"] == "cuda":
        use_nvidia_libraries()
    stay_awake()
    zpath = Path(args.zip)
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zpath) as z:
        job = json.loads(z.read("job.json"))
        name = next(n for n in z.namelist() if n.startswith("sound/"))
        z.extract(name, tmp)
        audio = Path(tmp) / name
        say(f"{job['title']} ({clock(job['sound'].get('duration'))}, {job['lines']} lines)")
        describe(opts, models)
        results = [hear_in_own_process(job, audio, m, opts) for m in models]
    out = zpath.with_name(zpath.stem + "-result.json")
    out.write_text(json.dumps({"item_id": job["item_id"], "sound_size": job["sound"]["size"], "results": results}))
    say(f"\nWrote {out}. Import it on the episode's admin page in the archiver (Import from your PC).")


def cmd_hear(args) -> None:   # the process of its own, for one model
    if args.device == "cuda":
        use_nvidia_libraries()
    job = json.loads(Path(args.job).read_text())
    opts = {"device": args.device, "workers": args.workers, "threads": args.threads, "batch": args.batch}
    result = hear(job, Path(args.audio), args.model, opts)
    Path(args.out).write_text(json.dumps(result))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):   # an episode's title may hold characters a console can't show
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Hear JRWI episodes on this computer for the JRWI archiver.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Use:")[1])
    p.add_argument("--server", help="the archiver's address, if not the one set up")
    sub = p.add_subparsers(dest="cmd", required=True)

    def hearing_options(sp, models):
        sp.add_argument("--models", default=models, help=f"comma-separated (default {models})")
        sp.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
        sp.add_argument("--batch", type=int, default=16, help="graphics card: pieces of speech heard together")
        sp.add_argument("--workers", type=int, default=0, help="processor: pieces heard at once")
        sp.add_argument("--threads", type=int, default=0, help="processor: threads for each")
        sp.add_argument("--keep", action="store_true", help="keep the downloaded sound afterwards")

    sub.add_parser("setup")
    sp = sub.add_parser("check")
    sp.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    sp = sub.add_parser("list")
    sp.add_argument("--all", action="store_true", help="every episode that can be heard, timed or not")
    sp = sub.add_parser("compare")
    sp.add_argument("posts", nargs="*", type=int)
    hearing_options(sp, DEFAULT_COMPARE)
    sp = sub.add_parser("run")
    sp.add_argument("--all", action="store_true", help="every episode, timed or not, not only those not timed yet")
    sp.add_argument("--again", action="store_true", help="with --all: also those the models have heard already")
    sp.add_argument("--count", type=int, default=0)
    hearing_options(sp, DEFAULT_RUN)
    sp = sub.add_parser("file")
    sp.add_argument("zip")
    hearing_options(sp, DEFAULT_RUN)
    sp = sub.add_parser("_hear")
    for a in ("job", "audio", "model", "out"):
        sp.add_argument(a)
    sp.add_argument("--device", default="cpu")
    sp.add_argument("--workers", type=int, default=1)
    sp.add_argument("--threads", type=int, default=4)
    sp.add_argument("--batch", type=int, default=16)
    sub.add_parser("_probe")
    args = p.parse_args()
    try:
        {"setup": cmd_setup, "check": cmd_check, "list": cmd_list, "compare": cmd_compare, "run": cmd_run,
         "file": cmd_file, "_hear": cmd_hear, "_probe": cmd_probe}[args.cmd](args)
    except KeyboardInterrupt:
        die("Stopped. Nothing was sent for the episode being heard; the same command carries on where it stopped.")


if __name__ == "__main__":
    main()
