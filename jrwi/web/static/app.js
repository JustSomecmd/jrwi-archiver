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
const live = document.querySelector("[data-live-status]");
if (live) {
  const tick = async () => {
    try {
      const r = await fetch("/api/status");
      if (!r.ok) return;
      const s = await r.json();
      live.querySelector("[data-phase]").textContent = s.running ? s.phase : "Idle";
      live.querySelector("[data-detail]").textContent = s.running ? s.detail || "" : "";
      const pct = s.progress && s.progress.total ? Math.round((100 * s.progress.done) / s.progress.total) : 0;
      const bar = live.querySelector("[data-bar]");
      bar.parentElement.hidden = !(s.running && s.progress.total);
      bar.style.width = pct + "%";
      live.querySelector("[data-count]").textContent =
        s.running && s.progress.total ? `${s.progress.done} of ${s.progress.total}` : "";
      if (live.dataset.wasRunning === "1" && !s.running) location.reload();
      live.dataset.wasRunning = s.running ? "1" : "0";
    } catch (e) {}
  };
  tick();
  setInterval(tick, 3000);
}
