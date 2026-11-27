// Local Redactor editor. Talks only to the localhost server that served it.
"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  state: null,
  words: [],          // [{i, t, s, e, el}]
  red: new Set(),     // redacted word indices
  undo: [], redo: [],
  player: null,
  nowIdx: -1,
  view: "redacted",   // "original" | "redacted"
  src: null,          // "/media" or "/output-media?id=<export folder>" (what the player shows)
  autoMuted: false,   // muted by the Redacted preview during a redacted word
  userMuted: false,   // muted with the player's mute button
  pv: { key: null, status: "idle" },  // face-blur preview from the server (see runPreview)
  curKey: null,
};
try { if (localStorage.getItem("view") === "original") S.view = "original"; } catch (_) {}

// ---------------------------------------------------------------- API ----
async function api(path, body) {
  const opt = body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}

async function pollJob(id, onDone) {
  $("job").hidden = false;
  for (;;) {
    const j = await api("/api/job/" + id);
    $("jobBar").style.width = (j.progress * 100).toFixed(1) + "%";
    $("jobMsg").textContent = j.message || "";
    if (j.status === "done") { onDone(j.result); return; }
    if (j.status === "error") throw new Error(j.error);
    await new Promise((r) => setTimeout(r, 400));
  }
}

// ------------------------------------------------------------ Loading ----
function storageKey() { return "redactions:" + (S.state?.transcript_path || ""); }
function persist() {
  try { localStorage.setItem(storageKey(), JSON.stringify([...S.red])); } catch (_) {}
}
function restore() {
  try { return new Set(JSON.parse(localStorage.getItem(storageKey()) || "[]")); } catch (_) { return new Set(); }
}

function fmt(t) {
  if (t == null) return "--:--";
  const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = (t % 60);
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + s.toFixed(1).padStart(4, "0");
}

function applyState(st) {
  const prev = S.state;
  if (prev && (prev.media !== st.media || prev.transcript_path !== st.transcript_path)) {
    // Different files: clear results and messages left from the previous ones.
    $("result").hidden = true; $("result").innerHTML = "";
    $("job").hidden = true; $("jobBar").style.width = "0"; $("jobMsg").textContent = "";
    $("findInput").value = ""; $("findInput").placeholder = "Find phrase…";
    $("playerLabel").textContent = "";
  }
  S.state = st;
  // Remembered so the same files reopen when the server restarts itself after a
  // code change (same launch). A fresh launch of the app starts blank.
  if (st.media || st.transcript_path) {
    try {
      localStorage.setItem("lastOpen", JSON.stringify({ session: st.session, export_dir: st.exports?.[0]?.dir,
        files: { media: st.media, transcript: st.transcript_path, out_root: st.out_root } }));
    } catch (_) {}
  }
  $("mediaPath").value = st.media || "";
  $("transcriptPath").value = st.transcript_path || "";
  $("outRoot").value = st.out_root || "";
  S.applying = true;  // refreshView waits until player, picks and redactions are all loaded
  try { setupPlayer(st.media_info); renderTranscript(st.transcript); } finally { S.applying = false; }
  refreshView();
}

function setupPlayer(info) {
  const v = $("player"), a = $("aplayer");
  v.hidden = a.hidden = true; v.removeAttribute("src"); a.removeAttribute("src");
  delete v.dataset.output; delete a.dataset.output; v.muted = a.muted = false;
  S.player = null; S.src = null; S.autoMuted = false;
  $("vControls").hidden = !info?.has_video;
  drawBlur([]);
  if (!info) { $("speakerPanel").hidden = true; S.picks = []; renderPicks(); return; }
  S.player = info.has_video ? v : a;
  S.player.hidden = false;
  loadSource("/media");
  setPicking(false);
  $("speakerPanel").hidden = !info.has_video;
  S.picks = restorePicks(); renderPicks();
}

// Swap what the player shows, keeping the playback position.
function loadSource(src) {
  const p = S.player;
  if (!p || S.src === src) return;
  const t = S.src ? p.currentTime : 0, playing = S.src && !p.paused;
  S.src = src;
  if (src.startsWith("/output-media")) { p.dataset.output = "1"; setPicking(false); } else delete p.dataset.output;
  S.autoMuted = false; applyMute();
  drawBlur([]);
  p.src = src + (src.includes("?") ? "&" : "?") + "v=" + Date.now();
  if (t || playing) {
    p.addEventListener("loadedmetadata", () => {
      p.currentTime = Math.min(t, p.duration || t);
      if (playing) p.play().catch(() => {});
    }, { once: true });
  }
  drawPickDots();
}

// ------------------------------------------------- Original / Redacted ----
function currentOptions() {
  return {
    pad_before: +$("padBefore").value, pad_after: +$("padAfter").value,
    blur_mode: $("blurMode").value, failsafe: $("failsafe").checked,
    speaker_picks: S.picks.map(({ t, x, y }) => ({ t, x, y })),
  };
}

// The newest export made from exactly what's marked now (same words, padding,
// blur setting and speaker picks), if there is one. Its file is what the
// Redacted view plays; otherwise the view falls back to a rough live preview.
function sameSettings(o) {
  return JSON.stringify([+o.pad_before, +o.pad_after, o.blur_mode, !!o.failsafe,
    (o.speaker_picks || []).map((p) => [+p.t, +p.x, +p.y])]);
}
// The export chosen with "Open export…" (or just made). The Redacted view always
// plays it; the label notes when the marks have changed since.
function chosenExport() {
  return (S.state?.exports || []).find((e) => e.has_media) || null;
}
function exportMatches(e) {
  return JSON.stringify([...e.redacted].sort((a, b) => a - b)) ===
    JSON.stringify([...S.red].sort((a, b) => a - b)) && sameSettings(e.options) === sameSettings(currentOptions());
}

// ----------------------------------------------------- Live preview ----
// Before an export, the Redacted view previews it. The server runs the same face
// detection and speaker tracking as the export (without writing a video) and
// returns the regions to blur per frame plus the exact mute spans; the page
// blurs just those regions on a canvas over the picture.
function previewKey() {
  return JSON.stringify([S.state?.media, S.state?.transcript_path,
    [...S.red].sort((a, b) => a - b), sameSettings(currentOptions())]);
}
function schedulePreview() {
  if (!S.state?.media || !S.state.transcript || !S.red.size || S.pv.key === S.curKey) return;
  const key = S.curKey;
  S.pv = { key, status: "waiting", progress: 0 };
  clearTimeout(S.pvTimer);
  S.pvTimer = setTimeout(() => runPreview(key), 500);  // wait for a burst of edits to settle
}
async function runPreview(key) {
  if (S.pv.key !== key) return;
  S.pv.status = "running";
  try {
    const { job } = await api("/api/preview", { redacted: [...S.red], options: currentOptions() });
    for (;;) {
      if (S.pv.key !== key) return;  // marks changed: a newer preview is on its way
      const j = await api("/api/job/" + job);
      if (S.pv.key !== key) return;
      if (j.status === "done") { Object.assign(S.pv, { status: "ready", data: j.result }); break; }
      if (j.status === "error") throw new Error(j.error);
      S.pv.progress = j.progress || 0;
      refreshView();
      await new Promise((r) => setTimeout(r, 250));
    }
  } catch (err) {
    if (S.pv.key === key) Object.assign(S.pv, { status: "error", error: err.message });
  }
  refreshView();
}
function previewData() {
  return S.pv.status === "ready" && S.pv.key === S.curKey ? S.pv.data : null;
}
// What to blur at time t, as [x0, y0, x1, y1] fractions of the frame.
function previewRegions(t) {
  const d = previewData();
  if (!d) return inRedaction(t, 0.25) ? [[0, 0, 1, 1]] : [];  // until faces are found: whole picture
  const f = d.frames, half = 0.5 / d.fps;
  let lo = 0, hi = f.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (f[mid][0] <= t + half) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans >= 0 && t - f[ans][0] < 3 * half ? f[ans][1] : [];
}
function previewMuted(t) {
  const d = previewData();
  return d ? d.spans.some(([a, b]) => t >= a && t <= b) : inRedaction(t);
}

// Blur regions of the picture on a canvas laid over it: shrink each region and
// scale it back up (the same idea as the export's pixelate-and-blur). Only the
// picture is covered; the controls sit below it.
function drawBlur(regions) {
  const c = $("blurCanvas");
  if (!regions.length && !S.blurDrawn) return;
  const v = $("player"), ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cw = Math.round(c.clientWidth * dpr), ch = Math.round(c.clientHeight * dpr);
  if (c.width !== cw || c.height !== ch) { c.width = cw; c.height = ch; }
  ctx.clearRect(0, 0, c.width, c.height);
  S.blurDrawn = false;
  if (!regions.length || !v.videoWidth) return;
  const vr = videoRect(), vw = v.videoWidth, vh = v.videoHeight;
  const tiny = S.tiny || (S.tiny = document.createElement("canvas")), tctx = tiny.getContext("2d");
  ctx.imageSmoothingEnabled = true;
  for (const [x0, y0, x1, y1] of regions) {
    const sw = (x1 - x0) * vw, sh = (y1 - y0) * vh;
    if (sw < 2 || sh < 2) continue;
    tiny.width = Math.max(1, Math.round(sw / 24)); tiny.height = Math.max(1, Math.round(sh / 24));
    tctx.drawImage(v, x0 * vw, y0 * vh, sw, sh, 0, 0, tiny.width, tiny.height);
    ctx.drawImage(tiny, 0, 0, tiny.width, tiny.height, (vr.ox + x0 * vr.w) * dpr, (vr.oy + y0 * vr.h) * dpr,
      (x1 - x0) * vr.w * dpr, (y1 - y0) * vr.h * dpr);
  }
  S.blurDrawn = true;
}

function setView(v) {
  S.view = v;
  try { localStorage.setItem("view", v); } catch (_) {}
  refreshView();
}

function refreshView() {
  const red = S.view === "redacted";
  $("viewOriginal").setAttribute("aria-pressed", String(!red));
  $("viewRedacted").setAttribute("aria-pressed", String(red));
  $("editor").classList.toggle("show-original", !red);
  if (!S.state || S.applying) return;
  const name = S.state.media ? S.state.media.split(/[\\/]/).pop() : "";
  S.curKey = previewKey();
  // Work out the face preview even in the Original view, so it's ready on switching.
  if (!S.state.media_is_export && !chosenExport()) schedulePreview();
  const ex = red && !S.state.media_is_export ? chosenExport() : null;
  if (S.player) loadSource(ex ? "/output-media?id=" + encodeURIComponent(ex.id) : "/media");
  const when = ex?.created ? new Date(ex.created).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
  let label = name, tip = S.state.media || "";
  if (S.state.media_is_export) {
    label = "Already-redacted copy · " + name;
    tip = "This file is an export, so it's played as is - no extra muting or blurring.";
  } else if (red) {
    tip = "";
    if (ex) {
      label = "Exported copy" + (when ? " · " + when : "");
      if (!exportMatches(ex)) {
        label += " · marks changed since";
        tip = "Your marks or settings have changed since this export. Export again to include them.";
      }
    } else if (!S.red.size) label = "Nothing redacted yet";
    else {
      const pv = S.pv, video = S.state.media_info?.has_video;
      if (!video || pv.status === "ready") {
        label = "Preview";
        tip = "Mutes and blurs exactly what an export with these marks and settings would.";
      } else if (pv.status === "error") {
        label = "Preview · whole frame blurred";
        tip = "Couldn't find faces for the preview (" + pv.error + "), so it blurs the whole picture.";
      } else {
        label = "Preview · finding faces" + (pv.progress ? " " + Math.round(pv.progress * 100) + "%" : "…");
        tip = "The whole picture is blurred during redactions until faces are found.";
      }
    }
  }
  if (!S.state.media && !S.state.transcript_path) label = tip = "";  // nothing open
  $("playerLabel").textContent = label;
  $("playerLabel").title = tip;
}

$("viewOriginal").onclick = () => setView("original");
$("viewRedacted").onclick = () => setView("redacted");
["padBefore", "padAfter", "blurMode", "failsafe"].forEach((id) => $(id).addEventListener("change", refreshView));

// ------------------------------------------------------ Speaker picks ----
// A pick is {t, x, y}: a moment in the video and a point on the speaker's face
// as fractions of the frame. Export blurs only the face nearest the pick and
// follows it frame to frame.
S.picks = [];
S.picking = false;

function picksKey() { return "picks:" + (S.state?.media || ""); }
function savePicks() { try { localStorage.setItem(picksKey(), JSON.stringify(S.picks)); } catch (_) {} }
function restorePicks() {
  try { return JSON.parse(localStorage.getItem(picksKey()) || "[]").filter((p) => isFinite(p.t)); } catch (_) { return []; }
}

// The video is letterboxed inside its element (object-fit: contain): map
// between element pixels and 0..1 frame fractions.
function videoRect() {
  const v = $("player"), r = v.getBoundingClientRect();
  const vw = v.videoWidth || 16, vh = v.videoHeight || 9;
  const sc = Math.min(r.width / vw, r.height / vh);
  return { r, w: vw * sc, h: vh * sc, ox: (r.width - vw * sc) / 2, oy: (r.height - vh * sc) / 2 };
}

function setPicking(on) {
  const v = $("player");
  S.picking = on && !v.hidden && !v.dataset.output;
  $("pickLayer").hidden = v.hidden;
  $("pickLayer").classList.toggle("active", S.picking);
  $("pickBtn").classList.toggle("on", S.picking);
  $("pickBtn").textContent = S.picking ? "Click the speaker's face… (Esc to cancel)" : "Pick speaker face";
  if (S.picking) v.pause();
  drawPickDots();
}

function drawPickDots() {
  const layer = $("pickLayer"), v = $("player");
  layer.replaceChildren();
  if (v.hidden || v.dataset.output || !S.picks.length) return;
  const vr = videoRect(), t = v.currentTime;
  for (const p of S.picks) {
    const dt = Math.abs(p.t - t);
    if (dt > 2) continue;
    const d = document.createElement("div");
    d.className = "pick-dot" + (dt > 0.3 ? " stale" : "");
    d.style.left = (vr.ox + p.x * vr.w) + "px";
    d.style.top = (vr.oy + p.y * vr.h) + "px";
    layer.appendChild(d);
  }
}

function renderPicks() {
  const list = $("pickList");
  list.replaceChildren();
  for (const p of S.picks) {
    const li = document.createElement("li");
    const t = document.createElement("span"); t.className = "t"; t.textContent = fmt(p.t);
    const txt = document.createElement("span"); txt.className = "txt";
    txt.textContent = `face at ${Math.round(p.x * 100)}% across, ${Math.round(p.y * 100)}% down`;
    const x = document.createElement("button"); x.className = "x"; x.textContent = "✕"; x.title = "Remove pick";
    x.onclick = (e) => { e.stopPropagation(); S.picks = S.picks.filter((q) => q !== p); savePicks(); renderPicks(); };
    li.append(t, txt, x);
    li.onclick = () => { if (S.player) { S.player.pause(); S.player.currentTime = p.t; } };
    list.appendChild(li);
  }
  $("pickCount").textContent = S.picks.length;
  drawPickDots();
  refreshView();
}

$("pickBtn").onclick = () => setPicking(!S.picking);
$("pickLayer").addEventListener("click", (e) => {
  if (!S.picking) return;
  const vr = videoRect();
  const x = (e.clientX - vr.r.left - vr.ox) / vr.w, y = (e.clientY - vr.r.top - vr.oy) / vr.h;
  if (x < 0 || x > 1 || y < 0 || y > 1) return;
  const t = Math.round($("player").currentTime * 100) / 100;
  // One pick per moment: re-clicking at the same time replaces it.
  S.picks = S.picks.filter((p) => Math.abs(p.t - t) > 0.05);
  S.picks.push({ t, x: Math.round(x * 1000) / 1000, y: Math.round(y * 1000) / 1000 });
  S.picks.sort((a, b) => a.t - b.t);
  $("blurMode").value = "speaker";
  savePicks(); renderPicks(); setPicking(false);
});
$("player").addEventListener("timeupdate", drawPickDots);
$("player").addEventListener("seeked", drawPickDots);
window.addEventListener("resize", drawPickDots);
document.addEventListener("fullscreenchange", drawPickDots);

function renderTranscript(tr) {
  const ed = $("editor");
  ed.innerHTML = "";
  S.words = []; S.undo = []; S.redo = [];
  if (!tr) {
    ed.innerHTML = '<p class="empty">No transcript loaded. Provide one, or click “Transcribe locally”.</p>';
    S.red = new Set(); updateRedactions(); return;
  }
  const frag = document.createDocumentFragment();
  for (const seg of tr.segments) {
    const p = document.createElement("p");
    p.className = "seg";
    if (seg.start != null || seg.speaker) {
      const h = document.createElement("span");
      h.className = "seg-head";
      h.contentEditable = "false";
      h.innerHTML = (seg.start != null ? fmt(seg.start) : "") + (seg.speaker ? " · <b></b>" : "");
      if (seg.speaker) h.querySelector("b").textContent = seg.speaker;
      p.appendChild(h);
    }
    seg.words.forEach((w, k) => {
      const sp = document.createElement("span");
      sp.className = "w";
      sp.dataset.i = w.i;
      sp.textContent = w.t;
      if (w.s != null) sp.title = fmt(w.s) + " – " + fmt(w.e);
      p.appendChild(sp);
      if (k < seg.words.length - 1) p.appendChild(document.createTextNode(" "));
      S.words[w.i] = { ...w, el: sp };
    });
    frag.appendChild(p);
  }
  ed.appendChild(frag);
  if (!tr.timed && S.state.media) {
    const warn = document.createElement("p");
    warn.className = "empty";
    warn.textContent = "⚠ This transcript has no timestamps, so audio/video can't be aligned. Use SRT/VTT/JSON or “Transcribe locally”.";
    ed.prepend(warn);
  }
  S.red = new Set([...restore()].filter((i) => i < S.words.length));
  updateRedactions();
}

// --------------------------------------------------------- Redaction ----
function selectedIndices() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return [];
  const out = new Set();
  for (let r = 0; r < sel.rangeCount; r++) {
    const range = sel.getRangeAt(r);
    if (!$("editor").contains(range.commonAncestorContainer)) continue;
    for (const w of S.words) {
      if (!w) continue;
      // A partially selected word counts as selected (whole word is redacted).
      if (range.intersectsNode(w.el.firstChild)) {
        const txt = w.el.firstChild;
        if (range.endContainer === txt && range.endOffset === 0) continue;
        if (range.startContainer === txt && range.startOffset === txt.length) continue;
        out.add(w.i);
      }
    }
  }
  return [...out].sort((a, b) => a - b);
}

function change(indices, on) {
  const changed = indices.filter((i) => S.red.has(i) !== on);
  if (!changed.length) return 0;
  S.undo.push({ indices: changed, on }); S.redo = [];
  changed.forEach((i) => on ? S.red.add(i) : S.red.delete(i));
  updateRedactions();
  return changed.length;
}

function undo() {
  const op = S.undo.pop(); if (!op) return;
  op.indices.forEach((i) => op.on ? S.red.delete(i) : S.red.add(i));
  S.redo.push(op); updateRedactions();
}
function redo() {
  const op = S.redo.pop(); if (!op) return;
  op.indices.forEach((i) => op.on ? S.red.add(i) : S.red.delete(i));
  S.undo.push(op); updateRedactions();
}

function runs() {
  const idx = [...S.red].sort((a, b) => a - b), out = [];
  for (const i of idx) {
    const last = out[out.length - 1];
    if (last && i === last[last.length - 1] + 1) last.push(i); else out.push([i]);
  }
  return out;
}

function updateRedactions() {
  for (const w of S.words) if (w) w.el.classList.toggle("red", S.red.has(w.i));
  const list = $("redList");
  list.innerHTML = "";
  const rs = runs();
  for (const run of rs) {
    const first = S.words[run[0]], last = S.words[run[run.length - 1]];
    const li = document.createElement("li");
    const t = document.createElement("span"); t.className = "t";
    t.textContent = first.s != null ? fmt(first.s) + "–" + fmt(last.e) : "#" + run[0];
    const txt = document.createElement("span"); txt.className = "txt";
    txt.textContent = run.map((i) => S.words[i].t).join(" ");
    const x = document.createElement("button"); x.className = "x"; x.textContent = "✕"; x.title = "Remove redaction";
    x.onclick = (e) => { e.stopPropagation(); change(run, false); };
    li.append(t, txt, x);
    li.onclick = () => { jumpTo(first); first.el.scrollIntoView({ block: "center", behavior: "smooth" }); };
    list.appendChild(li);
  }
  $("redCount").textContent = S.red.size;
  $("undoBtn").disabled = !S.undo.length;
  $("redoBtn").disabled = !S.redo.length;
  persist();
  refreshView();
}

function jumpTo(w) {
  w.el.classList.remove("flash"); void w.el.offsetWidth; w.el.classList.add("flash");
  if (S.player && w.s != null) S.player.currentTime = Math.max(0, w.s - 0.3);
}

// ---------------------------------------------------- Player linkage ----
function wordAt(t) {
  let lo = 0, hi = S.words.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1, w = S.words[mid];
    if (w.s == null) return -1;
    if (w.s <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans >= 0 && t <= S.words[ans].e + 0.05 ? ans : -1;
}

function inRedaction(t, extra = 0) {
  const pb = (+$("padBefore").value || 0) + extra, pa = (+$("padAfter").value || 0) + extra;
  for (const i of S.red) { const w = S.words[i]; if (w.s != null && t >= w.s - pb && t <= w.e + pa) return true; }
  return false;
}

function tick() {
  const p = S.player;
  if (p && !p.paused && S.words.length) {
    const t = p.currentTime, k = wordAt(t);
    if (k !== S.nowIdx) {
      S.words[S.nowIdx]?.el.classList.remove("now");
      S.nowIdx = k;
      if (k >= 0) S.words[k].el.classList.add("now");
    }
  }
  // Redacted view before export: mute and blur what the export would.
  const live = p && S.src === "/media" && S.view === "redacted" && S.red.size > 0 && !S.state?.media_is_export;
  if (p) {
    const mute = !!live && previewMuted(p.currentTime);
    if (mute !== S.autoMuted) { S.autoMuted = mute; applyMute(); }
  }
  drawBlur(live && p === $("player") ? previewRegions(p.currentTime) : []);
  requestAnimationFrame(tick);
}

// ------------------------------------------------------------ Events ----
function positionFloat() {
  const fb = $("floatBtn"), sel = window.getSelection();
  if (!sel || sel.isCollapsed || !selectedIndices().length) { fb.hidden = true; return; }
  const r = sel.getRangeAt(0).getBoundingClientRect();
  const host = $("editor").parentElement.getBoundingClientRect();
  fb.style.left = Math.max(8, r.left - host.left + r.width / 2 - 30) + "px";
  fb.style.top = (r.top - host.top - 30) + "px";
  fb.hidden = false;
}

function redactSelection(on = true) {
  const idx = selectedIndices();
  if (!idx.length) return;
  change(idx, on);
  window.getSelection().removeAllRanges();
  $("floatBtn").hidden = true;
}

document.addEventListener("selectionchange", () => requestAnimationFrame(positionFloat));
$("floatBtn").addEventListener("mousedown", (e) => { e.preventDefault(); redactSelection(true); });
$("redactBtn").onclick = () => redactSelection(true);
$("unredactBtn").onclick = () => redactSelection(false);
$("undoBtn").onclick = undo;
$("redoBtn").onclick = redo;

$("editor").addEventListener("click", (e) => {
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed) return;
  const el = e.target.closest(".w");
  if (el) jumpTo(S.words[+el.dataset.i]);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && S.picking) { setPicking(false); return; }
  if (e.target.matches("input, select, textarea")) return;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if ((e.ctrlKey || e.metaKey) && k === "y") { e.preventDefault(); redo(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (k === "r") { e.preventDefault(); redactSelection(true); }
  else if (k === "u") { e.preventDefault(); redactSelection(false); }
  else if (k === "v") { e.preventDefault(); setView(S.view === "redacted" ? "original" : "redacted"); }
  else if (k === " " && S.player && e.target === document.body) { e.preventDefault(); S.player.paused ? S.player.play() : S.player.pause(); }
});

$("openForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const media = $("mediaPath").value.trim();
  let transcript = $("transcriptPath").value.trim(), out_root = $("outRoot").value.trim();
  const st = S.state;
  // A new video starts clean: the transcript that belonged to the previous
  // video is dropped (one the user just chose themselves is kept).
  if (st?.media && media !== st.media && transcript === (st.transcript_path || "")) transcript = "";
  // The box shows the previous file's default output folder; when different
  // files are opened, let the server pick a fresh default instead of reusing it.
  if (st && out_root === (st.out_root || "") &&
      (media !== (st.media || "") || transcript !== (st.transcript_path || ""))) out_root = "";
  try {
    applyState(await api("/api/open", { media, transcript, out_root }));
  } catch (err) { alert(err.message); }
});

// "Browse…" opens the OS file dialog via the local server (a browser file
// input can't reveal the real path, and originals are read in place).
document.querySelectorAll(".browse").forEach((btn) => {
  btn.onclick = async () => {
    const input = $(btn.dataset.target);
    const start = input.value.trim() || $("mediaPath").value.trim() || $("transcriptPath").value.trim();
    btn.disabled = true;
    try {
      const { path } = await api("/api/pick", { kind: btn.dataset.kind, start });
      if (!path) return;  // cancelled
      input.value = path;
      // Open what was picked straight away. A folder alone opens nothing.
      if (btn.dataset.kind !== "folder" || S.state?.media || S.state?.transcript_path) {
        $("openForm").requestSubmit();
      }
    } catch (err) { alert(err.message); }
    finally { btn.disabled = false; }
  };
});

// "Open export…": show an export made earlier. Its original video and
// transcript open with the same marks and settings, so the Redacted view plays
// the exported file (and editing anything switches back to the live preview).
$("openExportBtn").onclick = async () => {
  const btn = $("openExportBtn");
  btn.disabled = true;
  try {
    const start = $("outRoot").value.trim() || $("mediaPath").value.trim();
    const { path } = await api("/api/pick", { kind: "export", start });
    if (!path) return;  // cancelled
    const { state, originals_found } = await api("/api/open-export", { folder: path });
    applyState(state);
    const ex = state.exports[0];
    if (originals_found && ex) {
      const o = ex.options || {};
      if (o.pad_before != null) $("padBefore").value = o.pad_before;
      if (o.pad_after != null) $("padAfter").value = o.pad_after;
      if (o.blur_mode) $("blurMode").value = o.blur_mode;
      if (o.failsafe != null) $("failsafe").checked = !!o.failsafe;
      S.picks = (o.speaker_picks || []).map(({ t, x, y }) => ({ t, x, y }));
      savePicks(); renderPicks();
      S.red = new Set(ex.redacted.filter((i) => i < S.words.length));
      S.undo = []; S.redo = [];
      updateRedactions();
      setView("redacted");
    } else {
      alert("The original video or transcript for this export has moved, so its redacted copy is open on its own.");
    }
  } catch (err) { alert(err.message); }
  finally { btn.disabled = false; }
};

$("transcribeBtn").onclick = async () => {
  try {
    if (!S.state?.media) {
      applyState(await api("/api/open", { media: $("mediaPath").value.trim(), out_root: $("outRoot").value.trim() }));
    }
    const { job } = await api("/api/transcribe", {});
    $("result").hidden = true;
    await pollJob(job, async () => applyState(await api("/api/state")));
  } catch (err) { showError(err); }
};

$("findForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("findInput").value.trim(); if (!q) return;
  try {
    const { indices } = await api("/api/find", { query: q });
    const n = change(indices, true);
    $("findInput").value = "";
    $("findInput").placeholder = indices.length ? `${n} word(s) redacted for “${q}”` : `No matches for “${q}”`;
  } catch (err) { alert(err.message); }
});

$("exportBtn").onclick = async () => {
  const btn = $("exportBtn");
  btn.disabled = true; $("result").hidden = true;
  try {
    const { job } = await api("/api/export", {
      redacted: [...S.red],
      options: currentOptions(),
    });
    await pollJob(job, async (res) => {
      S.state.exports = (await api("/api/state")).exports;
      showResult(res);
      refreshView();
    });
  } catch (err) { showError(err); }
  btn.disabled = false;
};

function showResult(res) {
  const r = $("result"); r.hidden = false; r.innerHTML = "";
  const ok = document.createElement("div"); ok.className = "ok";
  ok.textContent = "✓ Export complete — originals verified unchanged"; r.appendChild(ok);
  for (const [k, v] of Object.entries(res.outputs)) {
    const d = document.createElement("div"); d.textContent = k;
    const c = document.createElement("code"); c.textContent = v; d.appendChild(c); r.appendChild(d);
  }
  if (res.stats && res.stats.frames) {
    const s = document.createElement("div"); s.className = "muted";
    s.textContent = `Blurred ${res.stats.frames} frames (${res.stats.face_frames} by face, ${res.stats.fullframe_frames} full-frame) · detector: ${res.stats.detector}`;
    r.appendChild(s);
  }
  if (res.outputs.media) {
    const b = document.createElement("button"); b.className = "secondary"; b.style.marginTop = "6px";
    b.textContent = "Watch the redacted copy";
    b.onclick = () => setView("redacted");
    r.appendChild(b);
  }
}
function showError(err) {
  const r = $("result"); r.hidden = false; r.innerHTML = "";
  const d = document.createElement("div"); d.className = "err"; d.textContent = "✕ " + err.message; r.appendChild(d);
}

// ---------------------------------------------------------- Controls ----
// The video has its own control bar below the picture, so the preview blur
// (drawn over the picture) never covers the controls.
const ICON = {
  play: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l9-5.5z" fill="currentColor"/></svg>',
  pause: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5h3v11H4zM9 2.5h3v11H9z" fill="currentColor"/></svg>',
  sound: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6h3l4-3v10L5 10H2z" fill="currentColor"/><path d="M11 5.5a3.5 3.5 0 0 1 0 5M12.5 3.5a6 6 0 0 1 0 9" stroke="currentColor" fill="none" stroke-width="1.3"/></svg>',
  muted: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6h3l4-3v10L5 10H2z" fill="currentColor"/><path d="M11 6l4 4M15 6l-4 4" stroke="currentColor" stroke-width="1.4"/></svg>',
  full: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4" stroke="currentColor" fill="none" stroke-width="1.5"/></svg>',
};
function applyMute() {
  if (S.player) S.player.muted = S.userMuted || S.autoMuted;
  $("vMute").innerHTML = S.userMuted ? ICON.muted : ICON.sound;
  $("vMute").setAttribute("aria-label", S.userMuted ? "Unmute" : "Mute");
}
function clock(t) {
  t = isFinite(t) ? t : 0;
  return Math.floor(t / 60) + ":" + String(Math.floor(t % 60)).padStart(2, "0");
}
function syncControls() {
  const v = $("player");
  $("vPlay").innerHTML = v.paused ? ICON.play : ICON.pause;
  $("vPlay").setAttribute("aria-label", v.paused ? "Play" : "Pause");
  $("vTime").textContent = clock(v.currentTime) + " / " + clock(v.duration);
  if (!S.seeking) $("vSeek").value = v.duration ? Math.round(v.currentTime / v.duration * 1000) : 0;
}
["play", "pause", "timeupdate", "durationchange", "loadedmetadata", "seeked", "emptied"]
  .forEach((ev) => $("player").addEventListener(ev, syncControls));
$("vPlay").onclick = () => { const v = $("player"); v.paused ? v.play().catch(() => {}) : v.pause(); };
$("player").addEventListener("click", () => { if (!S.picking) $("vPlay").click(); });
$("vSeek").addEventListener("input", () => {
  const v = $("player"); S.seeking = true;
  if (v.duration) v.currentTime = $("vSeek").value / 1000 * v.duration;
});
$("vSeek").addEventListener("change", () => { S.seeking = false; });
$("vMute").onclick = () => { S.userMuted = !S.userMuted; applyMute(); };
$("vFull").innerHTML = ICON.full;
$("vFull").onclick = () => {
  const box = $("videoStage");
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else (box.requestFullscreen || box.webkitRequestFullscreen)?.call(box);
};
applyMute(); syncControls();

// ------------------------------------------------------------- Start ----
refreshView();
(async () => {
  let st = await api("/api/state");
  if (!st.media && !st.transcript_path) {
    let last = null;
    try { last = JSON.parse(localStorage.getItem("lastOpen") || "null"); } catch (_) {}
    if (last?.session && last.session === st.session) {
      try {
        st = last.export_dir ? (await api("/api/open-export", { folder: last.export_dir })).state
                             : await api("/api/open", last.files);
      }
      catch (_) { try { localStorage.removeItem("lastOpen"); } catch (_) {} }  // moved or deleted
    }
  }
  applyState(st);
})().catch(() => {});
requestAnimationFrame(tick);
