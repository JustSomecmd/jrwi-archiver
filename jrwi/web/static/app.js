// Small helpers for the archive pages. No framework.

// Forms that ask before they do something big (the pages run no inline script, so this isn't an onsubmit)
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (e) => {
    if (!window.confirm(form.dataset.confirm)) e.preventDefault();
  });
});

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
    // The video and the audio share one place: pick it up again in the one now shown
    document.querySelectorAll(`[data-pane="${btn.dataset.show}"] [data-progress]`).forEach((m) => resume(m));
  });
});

// Remember where you are in an episode, on the server, so the apps and the other version pick up from the same
// spot. Positions are in each post's own timeline; the server converts between video and audio.
let saving = Promise.resolve();
const save = (media, finished) => {
  saving = fetch(`/api/progress/${media.dataset.progress}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ position: media.currentTime, duration: media.duration || null, finished: !!finished }),
  }).catch(() => {});
  return saving;
};
const seek = (media, at) => {
  if (!(at > 5)) return;
  if (media.readyState >= 1) media.currentTime = at;
  else media.addEventListener("loadedmetadata", () => { media.currentTime = at; }, { once: true });
};
function resume(media) {
  saving.then(() => fetch(`/api/progress/${media.dataset.progress}`))
    .then((r) => (r && r.ok ? r.json() : null))
    .then((p) => { if (p && !p.finished && media.paused) seek(media, p.position); })
    .catch(() => {});
}
document.querySelectorAll("audio[data-progress], video[data-progress]").forEach((media) => {
  seek(media, parseFloat(media.dataset.start || "0"));
  let last = 0;
  media.addEventListener("timeupdate", () => {
    if (Math.abs(media.currentTime - last) > 15) { last = media.currentTime; save(media, false); }
  });
  media.addEventListener("pause", () => { if (!media.ended) save(media, false); });
  media.addEventListener("ended", () => save(media, true));
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
// A time in the browser's own time zone: "today at 16:29", "tomorrow at 02:15", "Sun 23 Nov at 16:29"
// Just the time of day, on the 24-hour clock, in the browser's own time zone
const timeOfDay = (iso) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const whenAt = (iso) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day(d) - day(new Date())) / 86400000);
  if (days === 0) return `today at ${time}`;
  if (days === 1) return `tomorrow at ${time}`;
  if (days === -1) return `yesterday at ${time}`;
  return `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} at ${time}`;
};
// Times written by the server in UTC, shown in the browser's own time zone
document.querySelectorAll("[data-when-at]").forEach((el) => { el.textContent = whenAt(el.dataset.whenAt); });
document.querySelectorAll("[data-clock]").forEach((el) => { el.textContent = timeOfDay(el.dataset.clock); });
document.querySelectorAll("[data-time-of-day]").forEach((el) => {
  const d = new Date(el.dataset.timeOfDay);
  if (!Number.isNaN(d.getTime())) el.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
});
// How long until then: "in 2 h 5 min", "in 4 min", "due now"
const whenIn = (iso) => {
  const m = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (m <= 0) return "due now";
  if (m < 60) return `in ${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `in ${h} h ${r} min` : `in ${h} h`;
};
const RESULT_WORDS = { ok: "ok", login_failed: "the login failed", blocked: "blocked by the site", error: "an error",
                       stopped: "stopped" };
const live = document.querySelector("[data-live-status]");
if (live) {
  const every = Number(live.dataset.every || 0);
  const everyText = every ? `checks every ${Number.isInteger(every) ? every : every.toFixed(1)} hour${every === 1 ? "" : "s"}` : "";
  const tick = async () => {
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      // What's happening now, and when the next check is: always the next one, also while a check runs
      const last = s.last_check_finished
        ? `last check finished ${whenAt(s.last_check_finished)}${s.last_result ? ` (${RESULT_WORDS[s.last_result] || s.last_result})` : ""}`
        : "";
      const phase = live.querySelector("[data-phase]");
      const sched = live.querySelector("[data-schedule]");
      if (s.running) {
        phase.textContent = s.phase;
        sched.textContent = [s.last_check_started ? `Started ${whenAt(s.last_check_started)}` : "",
                             s.next_check ? `next check ${timeOfDay(s.next_check)}` : ""].filter(Boolean).join(" · ");
      } else if (s.next_check) {
        phase.textContent = `Next check ${timeOfDay(s.next_check)}`;
        sched.textContent = [whenIn(s.next_check), everyText, last].filter(Boolean).join(" · ");
      } else {
        phase.textContent = "No checks scheduled";
        sched.textContent = ["Checks only run when you press Check now. Set a schedule in Settings.", last]
          .filter(Boolean).join(" · ");
      }
      live.querySelector("[data-detail]").textContent = s.running ? s.detail || "" : "";
      // While archiving, the bar follows the post being archived (its file downloading now), and the count says
      // how many posts are done. Other phases, such as re-reading saved pages, show how far the whole job is.
      const p = s.progress || {};
      const perPost = s.step ? s.step === "posts" : s.phase === "archiving posts";
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
      // "post 3 of 12": the one being worked on now
      live.querySelector("[data-count]").textContent =
        s.running && p.total ? `${p.unit || "step"} ${Math.min(p.done + 1, p.total)} of ${p.total}` : "";
      // Counts and sizes change while a check runs
      const set = (key, text) => document.querySelectorAll(`[data-stat="${key}"]`).forEach((el) => { el.textContent = text; });
      for (const k of ["archived", "items", "locked", "problems"]) set(k, String(s.counts[k]));
      set("archive_bytes", humanSize(s.disk.archive_bytes));
      set("free_bytes", humanSize(s.disk.free_bytes));
      // The last check's problem is old news once a new check is running
      if (s.running) document.querySelectorAll("[data-last-problem]").forEach((el) => { el.hidden = true; });
      // A check started or ended: reload, so the buttons (Check now or Stop), counts and tables are current
      if ((live.dataset.wasRunning === "1") !== s.running) location.reload();
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

// "...Continue reading" and "Read more": only shown when the text is cut off, and open it in place
const initClamps = (root) => root.querySelectorAll("[data-clamp]").forEach((el) => {
  const btn = el.nextElementSibling && el.nextElementSibling.matches("[data-unclamp]") ? el.nextElementSibling : null;
  const check = () => {
    if (el.scrollHeight > el.clientHeight + 4) { if (btn) btn.hidden = false; }
    else { el.classList.remove("clamp"); if (btn) btn.hidden = true; }
  };
  check();
  el.querySelectorAll("img").forEach((i) => i.addEventListener("load", check, { once: true }));
  if (btn) btn.addEventListener("click", () => { el.classList.remove("clamp"); btn.hidden = true; });
});
initClamps(document);

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
  const mute = box.querySelector("[data-mute]");
  if (mute) mute.addEventListener("click", () => {
    audio.muted = !audio.muted;
    mute.classList.toggle("muted", audio.muted);
    mute.setAttribute("aria-label", audio.muted ? "Unmute" : "Mute");
  });
  const speeds = [1, 1.25, 1.5, 1.75, 2, 0.75];
  let dragging = false;
  play.addEventListener("click", () => (audio.paused ? audio.play() : audio.pause()));
  audio.addEventListener("play", () => {
    play.classList.add("playing");
    play.setAttribute("aria-label", "Pause");
    document.querySelectorAll("audio").forEach((other) => { if (other !== audio) other.pause(); });  // one at a time
  });
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

// Comments under posts in the feed: "Load more comments", and the Most relevant / Newest menu
document.querySelectorAll("[data-comments]").forEach((box) => {
  const list = box.querySelector(".clist");
  const more = box.querySelector("[data-load-more]");
  const label = box.querySelector("[data-sort-label]");
  const load = async (offset, limit, replace) => {
    const r = await fetch(`/items/${box.dataset.comments}/comments-html?csort=${box.dataset.sort}&offset=${offset}&limit=${limit}`);
    if (!r.ok) return;
    const html = await r.text();
    const holder = document.createElement("div");
    holder.innerHTML = html;
    initClamps(holder);
    if (replace) list.replaceChildren(...holder.childNodes); else list.append(...holder.childNodes);
    box.dataset.offset = String(offset + limit);
    if (more) more.hidden = r.headers.get("X-More") !== "1";
  };
  if (more) more.addEventListener("click", () => load(Number(box.dataset.offset), 10, false));
  box.querySelectorAll("[data-csort]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    box.dataset.sort = a.dataset.csort;
    label.textContent = a.textContent;
    a.closest("details").open = false;
    load(0, 2, true);
  }));
});

// Picture viewer: tapping a post's picture, or a picture or PDF it carries, opens it full screen. Swipe, the arrows or
// the arrow keys go through all of them, a PDF page by page; tapping the picture zooms in; Esc, the X or the phone's
// back button closes it. Each entry is {src, file, name}, or {pdf, file, name} for a PDF whose pages are looked up
// (/api/preview) when the viewer gets to it.
const viewer = (() => {
  const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  let box, stage, img, noteBox, count, save, prevBtn, nextBtn, list = [], at = 0, pushed = false, touchX = null, asked = 0;
  const build = () => {
    box = document.createElement("div");
    box.className = "lb";
    box.hidden = true;
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Picture viewer");
    box.innerHTML = `
      <div class="lb-top"><span class="lb-count"></span>
        <a class="lb-btn lb-save" download title="Download">${svg('<path d="M12 4v11M7 10l5 5 5-5M4 20h16"/>')}</a>
        <button type="button" class="lb-btn lb-close" title="Close">${svg('<path d="M6 6l12 12M18 6L6 18"/>')}</button></div>
      <div class="lb-stage"><img alt=""><div class="lb-note" hidden><p></p><a class="pill" target="_blank" rel="noopener">Open the file</a></div></div>
      <button type="button" class="lb-nav lb-prev" title="Previous">${svg('<path d="M15 6l-6 6 6 6"/>')}</button>
      <button type="button" class="lb-nav lb-next" title="Next">${svg('<path d="M9 6l6 6-6 6"/>')}</button>`;
    document.body.append(box);
    stage = box.querySelector(".lb-stage");
    img = stage.querySelector("img");
    noteBox = stage.querySelector(".lb-note");
    count = box.querySelector(".lb-count");
    save = box.querySelector(".lb-save");
    prevBtn = box.querySelector(".lb-prev");
    nextBtn = box.querySelector(".lb-next");
    box.querySelector(".lb-close").addEventListener("click", () => close());
    prevBtn.addEventListener("click", () => show(at - 1, -1));
    nextBtn.addEventListener("click", () => show(at + 1, 1));
    stage.addEventListener("click", (e) => {
      if (e.target.closest(".lb-note")) return;
      if (e.target !== img) { if (!box.classList.contains("zoom")) close(); return; }
      zoom(e);
    });
    stage.addEventListener("touchstart", (e) => { touchX = e.touches.length === 1 ? e.touches[0].clientX : null; }, { passive: true });
    stage.addEventListener("touchend", (e) => {
      if (touchX === null || box.classList.contains("zoom")) return;
      const dx = e.changedTouches[0].clientX - touchX;
      if (Math.abs(dx) > 50) show(at + (dx < 0 ? 1 : -1), dx < 0 ? 1 : -1);
      touchX = null;
    });
    document.addEventListener("keydown", (e) => {
      if (box.hidden) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") show(at - 1, -1);
      else if (e.key === "ArrowRight") show(at + 1, 1);
    });
    window.addEventListener("popstate", () => { if (!box.hidden) { pushed = false; close(); } });
  };
  const unzoom = () => { box.classList.remove("zoom"); img.style.width = ""; };
  const zoom = (e) => {
    if (box.classList.contains("zoom")) { unzoom(); return; }
    const r = img.getBoundingClientRect();
    const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
    box.classList.add("zoom");
    img.style.width = Math.max(img.naturalWidth, stage.clientWidth * 2) + "px";
    stage.scrollLeft = fx * img.offsetWidth - stage.clientWidth / 2;
    stage.scrollTop = fy * img.offsetHeight - stage.clientHeight / 2;
  };
  // A PDF's pages, in place of the PDF in the list; coming back from the next entry lands on its last page
  const expand = async (i, dir) => {
    const it = list[i], mine = ++asked;
    count.textContent = `${it.name || "PDF"} · loading its pages…`;
    let pages = null;
    try {
      const r = await fetch(it.pdf, { credentials: "same-origin" });
      const p = r.ok ? await r.json() : {};
      if (p.pages && p.page_url) {
        pages = Array.from({ length: p.pages }, (_, k) => ({ src: p.page_url.replace("{n}", k + 1), file: it.file,
                                                              name: it.name, page: k + 1, of: p.pages }));
      }
    } catch (e) { /* shown as a note below */ }
    if (mine !== asked || box.hidden || list[i] !== it) return;   // moved on, or closed, meanwhile
    list.splice(i, 1, ...(pages || [{ note: "This PDF's pages can't be shown here.", file: it.file, name: it.name }]));
    show(dir < 0 && pages ? i + pages.length - 1 : i, dir);
  };
  const show = (n, dir = 1) => {
    if (!list.length) return;
    at = (n + list.length) % list.length;
    unzoom();
    const it = list[at];
    if (it.pdf) { expand(at, dir); return; }
    asked++;
    img.hidden = !!it.note;
    noteBox.hidden = !it.note;
    if (it.note) {
      img.removeAttribute("src");
      noteBox.querySelector("p").textContent = it.note;
      noteBox.querySelector("a").href = it.file;
    } else {
      img.src = it.src;
      img.alt = it.name || "";
    }
    save.href = it.file || it.src;
    save.setAttribute("download", it.name || "");
    const where = [it.name, it.of > 1 ? `page ${it.page} of ${it.of}` : ""].filter(Boolean).join(" · ");
    count.textContent = [list.length > 1 ? `${at + 1} / ${list.length}` : "", where].filter(Boolean).join(" · ");
    prevBtn.hidden = nextBtn.hidden = list.length < 2;
    // The next picture loads while this one is looked at
    const next = list[(at + 1) % list.length];
    if (next && next.src && list.length > 1) new Image().src = next.src;
  };
  const open = (entries, n) => {
    if (!box) build();
    list = entries.map((e) => (typeof e === "string" ? { src: e, file: e, name: "" } : { ...e }));
    box.hidden = false;
    document.documentElement.classList.add("lb-open");
    if (!pushed) { history.pushState({ viewer: true }, ""); pushed = true; }
    show(n);
  };
  const close = () => {
    if (!box || box.hidden) return;
    box.hidden = true;
    asked++;   // a PDF still being looked up is forgotten
    unzoom();
    img.removeAttribute("src");
    document.documentElement.classList.remove("lb-open");
    if (pushed) { pushed = false; history.back(); }
  };
  return { open };
})();

document.addEventListener("click", (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
  // Pictures in a post's grid or gallery
  const a = e.target.closest("a[data-lightbox]");
  if (a) {
    e.preventDefault();
    const group = [...document.querySelectorAll(`a[data-lightbox="${a.dataset.lightbox}"]`)];
    viewer.open(group.map((x) => (x.dataset.pdf ? { pdf: x.dataset.pdf, file: x.dataset.file, name: x.dataset.name }
      : { src: x.dataset.src || x.href, file: x.dataset.file || x.dataset.src || x.href, name: x.dataset.name || "" })),
      group.indexOf(a));
    return;
  }
  // Pictures inside a post's text (not ones that are links)
  const pic = e.target.closest(".post-info .rich img, .pbody .rich img");
  if (pic && !pic.closest("a")) {
    const pics = [...pic.closest(".rich").querySelectorAll("img")].filter((x) => !x.closest("a"));
    viewer.open(pics.map((x) => x.currentSrc || x.src), pics.indexOf(pic));
  }
});

// A select that applies itself as soon as it's changed, such as Home's tag picker
document.querySelectorAll("select[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));
