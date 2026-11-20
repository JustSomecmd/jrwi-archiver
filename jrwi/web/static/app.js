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
      // Whole posts done, plus how far the file downloading now has got
      const p = s.progress || {};
      const pct = p.total ? Math.min(100, (100 * (p.done + (p.current || 0))) / p.total) : 0;
      const bar = live.querySelector("[data-bar]");
      bar.parentElement.hidden = !s.running;
      bar.parentElement.classList.toggle("busy", s.running && !p.total);  // no total yet: just show it's working
      bar.style.width = pct.toFixed(1) + "%";
      live.querySelector("[data-count]").textContent =
        s.running && s.progress.total ? `${s.progress.done} of ${s.progress.total}` : "";
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
