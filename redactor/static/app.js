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
  src: null,          // "/media" or "/output-media" (what the player is showing)
  autoMuted: false,
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
  $("videoBox").classList.remove("preview-blur");
  S.player = null; S.src = null; S.autoMuted = false;
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
  if (src === "/output-media") { p.dataset.output = "1"; setPicking(false); } else delete p.dataset.output;
  p.muted = S.autoMuted = false;
  $("videoBox").classList.remove("preview-blur");
  p.src = src + "?v=" + Date.now();
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

// The exported file is shown only while it still matches what's marked now.
function exportIsCurrent() {
  const ex = S.state?.last_export;
  if (!ex || !ex.has_media) return false;
  const now = [...S.red].sort((a, b) => a - b);
  return JSON.stringify(ex.redacted) === JSON.stringify(now) &&
    JSON.stringify(ex.options) === JSON.stringify(currentOptions());
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
  const exported = red && exportIsCurrent();
  if (S.player) loadSource(exported ? "/output-media" : "/media");
  $("playerLabel").textContent = !red ? name
    : exported ? "Exported redacted copy"
    : S.red.size ? "Preview · exact result after export" : "Nothing redacted yet";
  $("playerLabel").title = !red ? (S.state.media || "") : "";
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
  // Redacted view before export: mute, and blur the frame, during redacted words.
  // Only flip mute when it changes, so the player's own mute button still works.
  if (p && S.src === "/media") {
    const live = S.view === "redacted" && S.red.size > 0;
    const mute = live && inRedaction(p.currentTime);
    if (mute !== S.autoMuted) { p.muted = mute; S.autoMuted = mute; }
    $("videoBox").classList.toggle("preview-blur", live && p === $("player") && inRedaction(p.currentTime, 0.25));
  }
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
      S.state.last_export = (await api("/api/state")).last_export;
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

// ------------------------------------------------------------- Start ----
refreshView();
api("/api/state").then(applyState).catch(() => {});
requestAnimationFrame(tick);
