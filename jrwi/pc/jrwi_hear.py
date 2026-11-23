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
(If the card still isn't used on Windows, put the NVIDIA libraries from Purfview's whisper-standalone-win on PATH,
or run with --device cpu.)

Use:
    python jrwi_hear.py setup              the archiver's address and API token (its Settings page shows the token)
    python jrwi_hear.py list               episodes whose transcript isn't timed yet
    python jrwi_hear.py compare [POST]     hear one episode once per model (small.en and medium.en), send all of
                                           them, and compare: the archiver keeps whichever found the most words
    python jrwi_hear.py run [--count N]    hear episodes not timed yet, newest first, sending each as it's done
    python jrwi_hear.py file JOB.zip       without the network: an "Export for your PC" file from an episode's
                                           admin page; writes a result file to import on the same page

Options: --models small.en,medium.en   --device auto|cuda|cpu   --batch 16 (graphics card)
         --workers 4 --threads 6 (processor: pieces heard at once, and threads for each)   --keep (keep the sound)
POST is the number at the end of an episode's address in the archiver (/items/123).
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

PROGRAM = "1.0"
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


def api(cfg: dict, method: str, path: str, body=None, timeout: float = 120):
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
            msg = "the archiver doesn't know this yet: update it to 0.14.0 or newer"
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
    """Where pip put NVIDIA's libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12), if it did."""
    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        return []
    out = []
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


def show_results(out: dict) -> None:
    say("")
    say(f"  {'Model':<12} {'Words found':>11} {'Lines placed':>16} {'Hearing':>9} {'Speed':>7} {'Memory':>8}")
    for t in out["tries"]:
        lines = (t.get("placed") or 0) + (t.get("unplaced") or 0)
        placed = f"{t.get('placed')} of {lines}" if t.get("placed") is not None else "-"
        mem = f"{t['peak_mb']} MB" if t.get("peak_mb") else "-"
        speed = f"{t['speed']}x" if t.get("speed") else "-"
        mark = "  <- in use" if t.get("used") else ""
        say(f"  {t['model']:<12} {pct(t.get('found')):>11} {placed:>16} {clock(t.get('took')):>9} {speed:>7} {mem:>8}{mark}")
        if t.get("error"):
            say(f"    {t['error']}")
    for t in out["tries"][1:]:
        c = t.get("compare")
        if c and c.get("both"):
            say(f"\n  {t['model']} against {c['model']}: of the {c['both']} lines both placed, half start within "
                f"{c['median']} s of each other, nine in ten within {c['p90']} s ({pct(c['within_1s'])} within a "
                f"second). {t['model']} placed {c['only_this']} lines the other didn't; {c['model']} placed "
                f"{c['only_other']}.")
    say("\nThe episode's admin page and Admin > Transcripts in the archiver show the same.")


def cmd_setup(args) -> None:
    cfg = load_config(args)
    default = cfg.get("server") or "http://box:8090"
    server = input(f"The archiver's address [{default}]: ").strip() or default
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    token = getpass.getpass("Its API token (Settings in the archiver's web app; it isn't shown as you type): ").strip()
    cfg = {"server": server.rstrip("/"), "token": token or cfg.get("token", "")}
    ping = api(cfg, "GET", "/api/ping")
    if "transcript_jobs" not in (ping.get("features") or []):
        die(f"The archiver ({ping.get('version')}) can't hand out episodes yet: update it to 0.14.0 or newer.")
    jobs = api(cfg, "GET", "/api/transcripts/jobs?limit=200")["jobs"]
    CONFIG.write_text(json.dumps(cfg, indent=1))
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass
    say(f"Connected to the archiver {ping.get('version')}. {len(jobs)} episode(s) not timed yet.")
    say(f"Saved to {CONFIG.name}. It holds the token: keep it to yourself.")


def cmd_list(args) -> None:
    jobs = api(load_config(args), "GET", "/api/transcripts/jobs?limit=200")["jobs"]
    if not jobs:
        say("Every episode with a transcript is timed.")
    for j in jobs:
        say(f"  {j['item_id']:>8}  {clock(j['sound'].get('duration')):>8}  {j['lines']:>5} lines  {j['title']}")


def one_job(cfg: dict, post: int | None) -> dict:
    if post:
        return api(cfg, "GET", f"/api/transcripts/jobs/{post}")
    jobs = api(cfg, "GET", "/api/transcripts/jobs?limit=1")["jobs"]
    if not jobs:
        die("Every episode with a transcript is timed. Give a post number to hear one again: compare 123")
    return jobs[0]


def cmd_compare(args) -> None:
    cfg = load_config(args)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    opts = options(args)
    if opts["device"] == "cuda":
        use_nvidia_libraries()
    job = one_job(cfg, args.post)
    say(f"{job['title']} ({clock(job['sound'].get('duration'))}, {job['lines']} lines)")
    describe(opts, models)
    audio = download(cfg, job)
    results = [hear_in_own_process(job, audio, m, opts) for m in models]
    say("  Sending what was heard; the archiver matches it to the transcript...")
    out = api(cfg, "POST", f"/api/transcripts/jobs/{job['item_id']}",
              {"item_id": job["item_id"], "sound_size": job["sound"]["size"], "results": results}, timeout=900)
    show_results(out)
    if not args.keep:
        audio.unlink(missing_ok=True)


def cmd_run(args) -> None:
    cfg = load_config(args)
    model = [m.strip() for m in args.models.split(",") if m.strip()][0]
    opts = options(args)
    if opts["device"] == "cuda":
        use_nvidia_libraries()
    describe(opts, [model])
    done = 0
    while not args.count or done < args.count:
        jobs = api(cfg, "GET", "/api/transcripts/jobs?limit=1")["jobs"]
        if not jobs:
            say("Every episode with a transcript is timed.")
            break
        job = jobs[0]
        say(f"\n{job['title']} ({clock(job['sound'].get('duration'))}, {job['lines']} lines)")
        audio = download(cfg, job)
        result = hear_in_own_process(job, audio, model, opts)
        out = api(cfg, "POST", f"/api/transcripts/jobs/{job['item_id']}",
                  {"item_id": job["item_id"], "sound_size": job["sound"]["size"], "results": [result]}, timeout=900)
        t = out["tries"][0]
        say(f"  {pct(t.get('found'))} of words found, {t.get('placed')} of "
            f"{(t.get('placed') or 0) + (t.get('unplaced') or 0)} lines placed" + (f": {t['error']}" if t.get("error") else ""))
        if not args.keep:
            audio.unlink(missing_ok=True)
        done += 1   # tried, timed or not: the archiver hands out another next
    say(f"\n{done} episode(s) done.")


def cmd_file(args) -> None:
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    opts = options(args)
    if opts["device"] == "cuda":
        use_nvidia_libraries()
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
    sub.add_parser("list")
    sp = sub.add_parser("compare")
    sp.add_argument("post", nargs="?", type=int)
    hearing_options(sp, "small.en,medium.en")
    sp = sub.add_parser("run")
    sp.add_argument("--count", type=int, default=0)
    hearing_options(sp, "small.en")
    sp = sub.add_parser("file")
    sp.add_argument("zip")
    hearing_options(sp, "small.en,medium.en")
    sp = sub.add_parser("_hear")
    for a in ("job", "audio", "model", "out"):
        sp.add_argument(a)
    sp.add_argument("--device", default="cpu")
    sp.add_argument("--workers", type=int, default=1)
    sp.add_argument("--threads", type=int, default=4)
    sp.add_argument("--batch", type=int, default=16)
    args = p.parse_args()
    try:
        {"setup": cmd_setup, "list": cmd_list, "compare": cmd_compare, "run": cmd_run, "file": cmd_file,
         "_hear": cmd_hear}[args.cmd](args)
    except KeyboardInterrupt:
        die("Stopped. Nothing was sent for the episode being heard.")


if __name__ == "__main__":
    main()
