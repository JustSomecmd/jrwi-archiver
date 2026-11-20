// Small helpers for the archive pages. No framework.

// Video/audio switch on episode pages
document.querySelectorAll("[data-switch]").forEach((sw) => {
  sw.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-show]");
    if (!btn) return;
    sw.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    document.querySelectorAll("[data-pane]").forEach((p) => {
      const on = p.dataset.pane === btn.dataset.show;
      p.hidden = !on;
      if (!on) p.querySelectorAll("video,audio").forEach((m) => m.pause());
    });
  });
});

// Remember where you are in audio, on the server, so the apps pick up from the same spot
document.querySelectorAll("audio[data-progress]").forEach((audio) => {
  const id = audio.dataset.progress;
  const start = parseFloat(audio.dataset.start || "0");
  if (start > 5) audio.addEventListener("loadedmetadata", () => { audio.currentTime = start; }, { once: true });
  let last = 0;
  const save = (finished) =>
    fetch(`/api/progress/${id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position: audio.currentTime, duration: audio.duration || null, finished: !!finished }),
    }).catch(() => {});
  audio.addEventListener("timeupdate", () => {
    if (Math.abs(audio.currentTime - last) > 15) { last = audio.currentTime; save(false); }
  });
  audio.addEventListener("pause", () => save(false));
  audio.addEventListener("ended", () => save(true));
});

// Live status on the admin page
const humanSize = (n) => {
  if (n === null || n === undefined || n === "") return "?";
  n = Number(n);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(1)} ${units[i]}`;
};
const live = document.querySelector("[data-live-status]");
if (live) {
  const tick = async () => {
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      live.querySelector("[data-phase]").textContent = s.running ? s.phase : "Idle";
      live.querySelector("[data-detail]").textContent = s.running ? s.detail || "" : "";
      // While archiving, the bar follows the post being archived (its file downloading now), and the count says
      // how many posts are done. Other phases, such as re-reading saved pages, show how far the whole job is.
      const p = s.progress || {};
      const perPost = s.phase === "archiving posts";
      const hasCurrent = p.current !== null && p.current !== undefined;
      let pct = 0;
      let busy = s.running && !p.total;  // no length known: just show it's working
      if (perPost) {
        busy = s.running && !hasCurrent;  // reading the post's page or comments, or between files
        pct = hasCurrent ? 100 * p.current : 0;
      } else if (p.total) {
        pct = (100 * (p.done + (p.current || 0))) / p.total;
      }
      const bar = live.querySelector("[data-bar]");
      bar.parentElement.hidden = !s.running;
      bar.parentElement.classList.toggle("busy", busy);
      bar.style.width = Math.min(100, pct).toFixed(1) + "%";
      live.querySelector("[data-count]").textContent =
        s.running && p.total ? (perPost ? `post ${p.done + 1} of ${p.total}` : `${p.done} of ${p.total}`) : "";
      // Counts and sizes change while a check runs
      const set = (key, text) => document.querySelectorAll(`[data-stat="${key}"]`).forEach((el) => { el.textContent = text; });
      for (const k of ["archived", "items", "locked", "problems"]) set(k, String(s.counts[k]));
      set("archive_bytes", humanSize(s.disk.archive_bytes));
      set("free_bytes", humanSize(s.disk.free_bytes));
      // The last check's problem is old news once a new check is running
      if (s.running) document.querySelectorAll("[data-last-problem]").forEach((el) => { el.hidden = true; });
      if (live.dataset.wasRunning === "1" && !s.running) location.reload();
      live.dataset.wasRunning = s.running ? "1" : "0";
    } catch (e) {}
  };
  tick();
  setInterval(tick, 3000);
}

// Close the Shows menu when tapping or clicking anywhere else
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.dd[open], details.mobnav[open]").forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  });
});

// Pictures are shown whole; the space around them takes the picture's average colour, like the site's audio posts
const averageColour = (img) => {
  try {
    const c = document.createElement("canvas");
    c.width = c.height = 12;
    const ctx = c.getContext("2d");
    ctx.drawImage(img, 0, 0, 12, 12);
    const d = ctx.getImageData(0, 0, 12, 12).data;
    let r = 0, g = 0, b = 0, n = 0;
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] < 16) continue;
      r += d[i]; g += d[i + 1]; b += d[i + 2]; n++;
    }
    if (!n) return null;
    const k = 0.8;  // a little darker, so white text and controls stay readable
    return `rgb(${Math.round((r / n) * k)}, ${Math.round((g / n) * k)}, ${Math.round((b / n) * k)})`;
  } catch (e) {
    return null;
  }
};
document.querySelectorAll("[data-avg]").forEach((box) => {
  const img = box.querySelector("img");
  if (!img) return;
  const paint = () => { const col = averageColour(img); if (col) box.style.backgroundColor = col; };
  if (img.complete && img.naturalWidth) paint();
  else img.addEventListener("load", paint, { once: true });
});

// "...Continue reading": only shown when the text is cut off, and opens it in place
document.querySelectorAll("[data-clamp]").forEach((el) => {
  const btn = el.nextElementSibling && el.nextElementSibling.matches("[data-unclamp]") ? el.nextElementSibling : null;
  const check = () => {
    if (el.scrollHeight > el.clientHeight + 4) { if (btn) btn.hidden = false; }
    else { el.classList.remove("clamp"); if (btn) btn.hidden = true; }
  };
  check();
  el.querySelectorAll("img").forEach((i) => i.addEventListener("load", check, { once: true }));
  if (btn) btn.addEventListener("click", () => { el.classList.remove("clamp"); btn.hidden = true; });
});

// Share: the original post on jrwishow.com
document.querySelectorAll("[data-share]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const url = btn.dataset.share;
    try {
      if (navigator.share) { await navigator.share({ title: btn.dataset.title || "", url }); return; }
      await navigator.clipboard.writeText(url);
      const label = btn.querySelector("span");
      const was = label.textContent;
      label.textContent = "Link copied";
      setTimeout(() => { label.textContent = was; }, 1800);
    } catch (e) {
      window.open(url, "_blank", "noopener");
    }
  });
});

// The audio player, laid out like the site's: speed, back and forward 15 seconds, play, times and a slider
const clock = (s) => {
  if (!isFinite(s) || s < 0) s = 0;
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
};
document.querySelectorAll("[data-aplayer]").forEach((box) => {
  const audio = box.querySelector("audio");
  const play = box.querySelector("[data-play]");
  const seek = box.querySelector("[data-seek]");
  const cur = box.querySelector("[data-cur]");
  const dur = box.querySelector("[data-dur]");
  const speed = box.querySelector("[data-speed]");
  const speeds = [1, 1.25, 1.5, 1.75, 2, 0.75];
  let dragging = false;
  play.addEventListener("click", () => (audio.paused ? audio.play() : audio.pause()));
  audio.addEventListener("play", () => { play.classList.add("playing"); play.setAttribute("aria-label", "Pause"); });
  audio.addEventListener("pause", () => { play.classList.remove("playing"); play.setAttribute("aria-label", "Play"); });
  box.querySelectorAll("[data-skip]").forEach((b) => b.addEventListener("click", () => {
    audio.currentTime = Math.max(0, Math.min((audio.duration || 0), audio.currentTime + Number(b.dataset.skip)));
  }));
  speed.addEventListener("click", () => {
    const next = speeds[(speeds.indexOf(audio.playbackRate) + 1) % speeds.length] || 1;
    audio.playbackRate = next;
    speed.textContent = `${next}x`;
  });
  const show = () => {
    if (isFinite(audio.duration)) dur.textContent = clock(audio.duration);
    cur.textContent = clock(audio.currentTime);
    if (!dragging && audio.duration) seek.value = Math.round((1000 * audio.currentTime) / audio.duration);
  };
  audio.addEventListener("loadedmetadata", show);
  audio.addEventListener("timeupdate", show);
  seek.addEventListener("input", () => {
    dragging = true;
    if (audio.duration) cur.textContent = clock((seek.value / 1000) * audio.duration);
  });
  seek.addEventListener("change", () => {
    if (audio.duration) audio.currentTime = (seek.value / 1000) * audio.duration;
    dragging = false;
  });
});
