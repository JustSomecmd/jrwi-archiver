// The file inspector: shows what's inside each of a post's files, using /api/preview for PDFs, spreadsheets,
// documents, text and zip files. Everything from the files is put on the page as text, never as HTML.
(() => {
  const root = document.querySelector("[data-inspector]");
  if (!root) return;
  const view = root.querySelector("[data-view]");
  const buttons = [...root.querySelectorAll(".insp-file")];
  const TYPE = {pdf: "PDF", table: "Spreadsheet", document: "Document", text: "Text", archive: "Zip archive",
                image: "Picture", audio: "Audio", video: "Video", youtube: "YouTube", link: "Link", other: "File"};

  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === false || v == null) continue;
      if (k === "text") n.textContent = v; else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids) if (k != null) n.append(k);
    return n;
  };
  const size = (b) => {
    if (!b) return "";
    const u = ["B", "KB", "MB", "GB"]; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (i ? b.toFixed(1) : b) + " " + u[i];
  };
  const note = (text) => el("p", {class: "muted small", text});

  function header(f) {
    const actions = el("div", {class: "insp-actions"});
    if (f.url) {
      actions.append(el("a", {class: "pill", href: f.url, target: "_blank", rel: "noopener", text: "Open on its own"}));
      actions.append(el("a", {class: "pill", href: f.url, download: f.name, text: "Download"}));
    }
    if (f.source) actions.append(el("a", {class: "pill", href: f.source, target: "_blank", rel: "noopener", text: "Original link"}));
    return el("div", {class: "insp-head"},
      el("h2", {text: f.name}),
      el("div", {class: "muted small", text: [TYPE[f.type] || "File", f.mime, size(f.size)].filter(Boolean).join(" · ")}),
      actions);
  }

  function pdf(p, f, body) {
    if (!p.page_url || !p.pages) {
      body.append(el("iframe", {class: "insp-pdf", src: f.url, title: f.name}));
      return;
    }
    body.append(note(p.pages === 1 ? "1 page" : `${p.pages} pages`));
    for (let n = 1; n <= p.pages; n++) {
      body.append(el("figure", {class: "insp-page"},
        el("img", {src: p.page_url.replace("{n}", n), alt: `Page ${n}`, loading: n > 2 ? "lazy" : false}),
        el("figcaption", {class: "muted small", text: `Page ${n}`})));
    }
  }

  function table(p, body) {
    if (p.note) body.append(note(p.note));
    const sheets = p.sheets || [];
    if (!sheets.length) { if (!p.note) body.append(note("The spreadsheet is empty.")); return; }
    const tabs = el("div", {class: "insp-tabs"});
    const holder = el("div", {class: "insp-table"});
    const showSheet = (i) => {
      [...tabs.children].forEach((b, j) => b.classList.toggle("on", i === j));
      const s = sheets[i];
      const t = el("table");
      s.rows.forEach((r, ri) => t.append(el("tr", {}, el("th", {class: "rownum", text: String(ri + 1)}),
        ...r.map((c) => el("td", {text: c})))));
      holder.replaceChildren(s.rows.length ? t : note("This sheet is empty."));
      if (s.truncated) holder.append(note("Only the first rows and columns are shown. Download the file for all of it."));
    };
    sheets.forEach((s, i) => tabs.append(el("button", {type: "button", class: "pill", text: s.name || `Sheet ${i + 1}`})));
    tabs.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) showSheet([...tabs.children].indexOf(b)); });
    if (sheets.length > 1) body.append(tabs);
    body.append(holder);
    showSheet(0);
  }

  function documentBlocks(p, body) {
    if (p.note) body.append(note(p.note));
    const doc = el("div", {class: "rich insp-doc"});
    let list = null;
    for (const b of p.blocks || []) {
      if (b.kind === "item") {
        if (!list) { list = el("ul"); doc.append(list); }
        list.append(el("li", {text: b.text}));
        continue;
      }
      list = null;
      if (b.kind === "heading") doc.append(el(`h${Math.min(6, (b.level || 1) + 1)}`, {text: b.text}));
      else if (b.kind === "row") doc.append(el("p", {class: "insp-row", text: b.cells.join("  |  ")}));
      else doc.append(el("p", {text: b.text}));
    }
    body.append(doc.childElementCount ? doc : note("The document has no text."));
  }

  function text(p, body) {
    body.append(el("pre", {class: "insp-text", text: p.text || ""}));
    if (p.truncated) body.append(note("Only the start is shown. Download the file for all of it."));
  }

  function archive(p, body) {
    if (p.note) body.append(note(p.note));
    const entries = p.entries || [];
    body.append(note(`${entries.length}${p.truncated ? "+" : ""} entries`));
    const t = el("table", {class: "insp-zip"});
    for (const e of entries) t.append(el("tr", {}, el("td", {text: e.name}), el("td", {class: "muted", text: e.folder ? "folder" : size(e.size)})));
    body.append(t);
  }

  async function show(btn, tapped = false) {
    buttons.forEach((b) => b.classList.toggle("on", b === btn));
    const f = JSON.parse(btn.dataset.file);
    const key = f.path || f.url || f.source || "";
    history.replaceState(null, "", `?f=${encodeURIComponent(key)}`);
    const body = el("div", {class: "insp-body"});
    view.replaceChildren(header(f), body);
    if (tapped && window.matchMedia("(max-width: 900px)").matches) view.scrollIntoView({behavior: "smooth", block: "start"});
    switch (f.type) {
      case "image":
        body.append(el("a", {href: f.url, "data-lightbox": "insp"}, el("img", {class: "insp-img", src: f.url, alt: f.name})));
        return;
      case "audio":
        body.append(el("audio", {controls: true, preload: "metadata", src: f.url}));
        return;
      case "video":
        body.append(el("video", {controls: true, preload: "metadata", src: f.url, class: "insp-video"}));
        return;
      case "youtube":
        if (f.youtube_id) {
          body.append(el("div", {class: "insp-yt"}, el("iframe", {src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(f.youtube_id)}`,
            allow: "encrypted-media; picture-in-picture", allowfullscreen: true, title: f.name})));
        }
        body.append(note("YouTube videos aren't saved in the archive. TubeArchivist keeps JRWI's YouTube channel."));
        return;
      case "link":
        body.append(note("This Google link couldn't be saved (a whole Drive folder can't be downloaded, for example). The original link may still work."));
        return;
    }
    if (!f.preview) {
      body.append(note("There's no preview for this type of file. Download it to open it."));
      return;
    }
    body.append(note("Loading…"));
    let p;
    try {
      const r = await fetch(f.preview, {credentials: "same-origin"});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      p = await r.json();
    } catch (e) {
      body.replaceChildren(note(`The preview couldn't be loaded (${e.message}). Download the file instead.`));
      return;
    }
    body.replaceChildren();
    ({pdf: () => pdf(p, f, body), table: () => table(p, body), document: () => documentBlocks(p, body),
      text: () => text(p, body), archive: () => archive(p, body)}[p.type] || (() => body.append(note("No preview."))))();
  }

  root.addEventListener("click", (e) => { const b = e.target.closest(".insp-file"); if (b) show(b, true); });
  const pick = root.dataset.pick;
  const first = buttons.find((b) => { const f = JSON.parse(b.dataset.file); return pick && [f.path, f.url, f.source].includes(pick); })
    || buttons[0];
  if (first) show(first);
})();
