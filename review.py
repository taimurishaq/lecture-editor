"""Caption review page: the lecture video next to its captions, in your browser.

Runs a small web server on this computer only (127.0.0.1), opens the page, and saves
your edits back to work/<name>.srt. Nothing is uploaded anywhere.

    python edit.py review lecture1 [lecture2 ...]
"""
from __future__ import annotations

import json
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


# ----------------------------------------------------------------------------
# SRT reading and writing
# ----------------------------------------------------------------------------

def parse_srt(text: str) -> list[dict]:
    captions = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        rows = block.split("\n")
        for i, row in enumerate(rows):
            if "-->" in row:
                times = TIME.findall(row)
                if len(times) == 2:
                    start, end = (int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000
                                  for h, m, s, ms in times)
                    captions.append({"start": start, "end": end, "text": "\n".join(rows[i + 1:]).strip()})
                break
    return captions


def srt_stamp(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(captions: list[dict]) -> str:
    out, n = [], 0
    for c in sorted(captions, key=lambda c: c["start"]):
        text = "\n".join(line.strip() for line in str(c.get("text", "")).strip().splitlines() if line.strip())
        if not text:
            continue                                   # an emptied caption is removed
        n += 1
        out.append(f"{n}\n{srt_stamp(float(c['start']))} --> {srt_stamp(float(c['end']))}\n{text}\n")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Web server (this computer only)
# ----------------------------------------------------------------------------

class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, work: Path, stems: list[str], max_chars: int):
        super().__init__(("127.0.0.1", 0), Handler)
        self.work, self.stems, self.max_chars = work, stems, max_chars
        self.done = threading.Event()
        self.save_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"


class Handler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, *_args) -> None:          # keep the terminal quiet
        pass

    # -- helpers
    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stem_param(self) -> str | None:
        stem = parse_qs(urlparse(self.path).query).get("stem", [""])[0]
        return stem if stem in self.server.stems else None

    # -- routes
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/lectures":
            self.send_json({"lectures": self.server.stems, "max_chars": self.server.max_chars})
        elif path == "/api/captions":
            stem = self.stem_param()
            if not stem:
                return self.send_json({"error": "unknown lecture"}, 404)
            srt = self.server.work / f"{stem}.srt"
            text = srt.read_text(encoding="utf-8-sig", errors="replace") if srt.exists() else ""
            self.send_json({"captions": parse_srt(text), "video": "/video/" + quote(stem)})
        elif path.startswith("/video/"):
            stem = unquote(path[len("/video/"):])
            if stem not in self.server.stems:
                return self.send_error(HTTPStatus.NOT_FOUND)
            self.send_video(self.server.work / f"{stem}_fast.mp4")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/api/save":
            stem = self.stem_param()
            if not stem:
                return self.send_json({"error": "unknown lecture"}, 404)
            try:
                captions = json.loads(raw.decode("utf-8"))["captions"]
                text = format_srt(captions)
            except (ValueError, KeyError, TypeError) as e:
                return self.send_json({"error": f"could not read the captions: {e}"}, 400)
            srt = self.server.work / f"{stem}.srt"
            with self.server.save_lock:
                backup = srt.with_name(srt.name + ".before-review")
                if srt.exists() and not backup.exists():
                    backup.write_bytes(srt.read_bytes())     # first save keeps the machine version
                tmp = srt.with_name(srt.name + ".saving")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(srt)
            self.send_json({"ok": True, "count": text.count("-->")})
        elif path == "/api/done":
            self.send_json({"ok": True})
            self.server.done.set()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def send_video(self, file: Path) -> None:
        """Serve the video with byte ranges, so the browser can jump to any time."""
        if not file.exists():
            return self.send_error(HTTPStatus.NOT_FOUND, "Video not found; run process first.")
        size = file.stat().st_size
        start, end = 0, size - 1
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if rng and (rng[1] or rng[2]):
            if rng[1]:
                start = int(rng[1])
                end = min(int(rng[2]), size - 1) if rng[2] else size - 1
            else:                                         # last N bytes
                start = max(0, size - int(rng[2]))
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(file, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass                                          # the browser jumped elsewhere; normal


def start_server(work: Path, stems: list[str], max_chars: int, open_browser: bool = True) -> ReviewServer:
    server = ReviewServer(work, stems, max_chars)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if open_browser:
        webbrowser.open(server.url)
    return server


# ----------------------------------------------------------------------------
# The page (all inline: works offline)
# ----------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caption review</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #667085; --line: #e3e6ec;
  --accent: #2563eb; --accent-soft: #e8efff; --warn: #b45309; --ok: #15803d; --field: #ffffff;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161b; --panel: #1c1f26; --text: #e6e8ee; --muted: #9aa3b2; --line: #2c313b;
    --accent: #6ea0ff; --accent-soft: #1f2b45; --warn: #f0a443; --ok: #4ade80; --field: #161920;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.45 "Segoe UI", system-ui, -apple-system, sans-serif; }
header { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 12px;
  flex-wrap: wrap; padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--line); }
header h1 { font-size: 17px; margin: 0 8px 0 0; }
select, input[type=text] { font: inherit; color: var(--text); background: var(--field);
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; }
button { font: inherit; cursor: pointer; border-radius: 6px; padding: 6px 12px;
  border: 1px solid var(--line); background: var(--panel); color: var(--text); }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.status { font-size: 13px; color: var(--muted); }
.status.saving { color: var(--warn); }
.status.saved { color: var(--ok); }
.status.error { color: #dc2626; font-weight: 600; }
.spacer { flex: 1; }
main { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 16px;
  padding: 16px; max-width: 1500px; margin: 0 auto; }
.left { position: sticky; top: 70px; align-self: start; }
.player { position: relative; background: #000; border-radius: 8px; overflow: hidden; }
video { width: 100%; display: block; max-height: 62vh; aspect-ratio: 16 / 9; background: #000; }
.overlay { position: absolute; left: 6%; right: 6%; bottom: 52px; text-align: center; pointer-events: none;
  color: #fff; font-weight: 600; font-size: clamp(14px, 2.1vw, 22px); white-space: pre-line;
  text-shadow: -2px -2px 0 #000, 2px -2px 0 #000, -2px 2px 0 #000, 2px 2px 0 #000, 0 0 4px #000; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin-top: 12px; }
.card h2 { font-size: 14px; margin: 0 0 8px; }
.help { color: var(--muted); font-size: 13px; margin: 0; }
.help kbd { border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 4px; padding: 0 4px;
  font-size: 12px; background: var(--bg); }
.find { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.find input { flex: 1 1 140px; min-width: 0; }
.list { display: flex; flex-direction: column; gap: 6px; }
.row { display: grid; grid-template-columns: 74px minmax(0, 1fr); gap: 8px; align-items: start;
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px; }
.row.current { border-color: var(--accent); background: var(--accent-soft); }
.row.changed .time::after { content: " \2022"; color: var(--warn); }
.time { font: 13px/1.2 Consolas, monospace; padding: 6px 4px; color: var(--accent); background: none;
  border: 1px solid transparent; text-align: left; }
.time small { display: block; color: var(--muted); margin-top: 2px; }
textarea { width: 100%; resize: none; overflow: hidden; font: inherit; color: var(--text);
  background: var(--field); border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; min-height: 36px; }
textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
textarea.empty { border-style: dashed; }
.meta { grid-column: 2; display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
.meta .long { color: var(--warn); font-weight: 600; }
mark { background: #fde68a; color: #000; }
.done { text-align: center; padding: 60px 16px; }
@media (max-width: 860px) {
  main { grid-template-columns: minmax(0, 1fr); padding: 12px; }
  header { padding: 10px 12px; }
  .left { position: static; }
}
</style>
</head>
<body>
<header>
  <h1>Caption review</h1>
  <select id="lecture" aria-label="Lecture"></select>
  <span id="status" class="status">Loading...</span>
  <span class="spacer"></span>
  <button id="save">Save</button>
  <button id="finish" class="primary">Finish review</button>
</header>
<main id="main">
  <section class="left">
    <div class="player">
      <video id="video" controls preload="metadata"></video>
      <div id="overlay" class="overlay"></div>
    </div>
    <div class="card">
      <p class="help">Click a time to jump there. Fix the words on the right; the video shows your text.
        Empty a caption to remove it. Changes save by themselves.<br>
        <kbd>Ctrl</kbd>+<kbd>Enter</kbd> in a caption replays it &middot;
        <kbd>Ctrl</kbd>+<kbd>Space</kbd> play / pause &middot; <kbd>Ctrl</kbd>+<kbd>S</kbd> save</p>
      <label class="help" style="display:block;margin-top:8px">
        <input type="checkbox" id="follow" checked> Scroll the list to follow the video</label>
    </div>
    <div class="card">
      <h2>Find and replace</h2>
      <div class="find">
        <input type="text" id="findText" placeholder="Find (e.g. a misheard word)">
        <input type="text" id="replaceText" placeholder="Replace with">
        <button id="replaceAll">Replace all</button>
      </div>
      <p class="help" id="findInfo" style="margin-top:6px"></p>
    </div>
  </section>
  <section class="list" id="list" aria-label="Captions"></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const video = $("video"), list = $("list"), overlay = $("overlay"), statusEl = $("status");
let lectures = [], maxChars = 84, stem = null, caps = [], original = [];
let dirty = false, saveTimer = null, saving = null, currentIdx = -1;

function fmt(t) {
  const m = Math.floor(t / 60), s = t - m * 60;
  const h = Math.floor(m / 60);
  const ss = s.toFixed(1).padStart(4, "0");
  return h ? `${h}:${String(m % 60).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function setStatus(text, cls) { statusEl.textContent = text; statusEl.className = "status " + (cls || ""); }

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

async function init() {
  const info = await api("/api/lectures");
  lectures = info.lectures; maxChars = info.max_chars;
  const sel = $("lecture");
  sel.innerHTML = "";
  for (const l of lectures) sel.add(new Option(l, l));
  sel.hidden = lectures.length < 2;
  await load(lectures[0]);
}

async function load(name) {
  if (dirty) await save();
  stem = name; $("lecture").value = name;
  const data = await api("/api/captions?stem=" + encodeURIComponent(name));
  caps = data.captions; original = caps.map(c => c.text);
  video.src = data.video; currentIdx = -1;
  document.title = "Captions: " + name;
  render();
  setStatus(`${caps.length} captions. Changes save automatically.`);
}

function autosize(t) { t.style.height = "auto"; t.style.height = t.scrollHeight + 2 + "px"; }

function render() {
  list.innerHTML = "";
  const needle = $("findText").value.trim().toLowerCase();
  caps.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "row"; row.dataset.i = i;
    const time = document.createElement("button");
    time.className = "time"; time.title = "Play from here";
    time.innerHTML = `${fmt(c.start)}<small>${(c.end - c.start).toFixed(1)}s</small>`;
    time.onclick = () => playFrom(i);
    const ta = document.createElement("textarea");
    ta.value = c.text; ta.rows = 1; ta.spellcheck = true;
    ta.setAttribute("aria-label", "Caption at " + fmt(c.start));
    const meta = document.createElement("div"); meta.className = "meta";
    const count = document.createElement("span");
    const hint = document.createElement("span");
    meta.append(hint, count);
    const refresh = () => {
      const len = ta.value.replace(/\n/g, " ").length;
      count.textContent = `${len} / ${maxChars}`;
      count.className = len > maxChars ? "long" : "";
      hint.textContent = ta.value.trim() ? (needle && ta.value.toLowerCase().includes(needle) ? "contains the search word" : "")
                                         : "empty: will be removed";
      ta.classList.toggle("empty", !ta.value.trim());
      row.classList.toggle("changed", ta.value !== original[i]);
    };
    ta.addEventListener("input", () => {
      c.text = ta.value; autosize(ta); refresh(); markDirty();
      if (i === currentIdx) overlay.textContent = c.text;
    });
    ta.addEventListener("focus", () => { if (video.paused) seekTo(i); });
    ta.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.key === "Enter") { e.preventDefault(); playFrom(i); }
    });
    row.append(time, ta, meta);
    list.append(row);
    refresh();
    requestAnimationFrame(() => autosize(ta));
  });
}

function seekTo(i) { video.currentTime = caps[i].start + 0.01; showCurrent(); }
function playFrom(i) { seekTo(i); video.play(); }

function indexAt(t) {
  let lo = 0, hi = caps.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (caps[mid].start <= t) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found >= 0 && t < caps[found].end + 0.05 ? found : -1;
}

function showCurrent() {
  const i = indexAt(video.currentTime);
  overlay.textContent = i >= 0 ? caps[i].text : "";
  if (i === currentIdx) return;
  const rows = list.children;
  if (currentIdx >= 0 && rows[currentIdx]) rows[currentIdx].classList.remove("current");
  currentIdx = i;
  if (i >= 0 && rows[i]) {
    rows[i].classList.add("current");
    if ($("follow").checked && !video.paused && !list.contains(document.activeElement)) {
      rows[i].scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}
video.addEventListener("timeupdate", showCurrent);
video.addEventListener("seeked", showCurrent);
let raf = null;
video.addEventListener("play", () => { const tick = () => { showCurrent(); raf = requestAnimationFrame(tick); }; tick(); });
video.addEventListener("pause", () => cancelAnimationFrame(raf));

function markDirty() {
  dirty = true; setStatus("Unsaved changes...", "saving");
  clearTimeout(saveTimer); saveTimer = setTimeout(save, 1200);
}

async function save() {
  clearTimeout(saveTimer);
  if (saving) await saving;
  if (!dirty) return;
  dirty = false; setStatus("Saving...", "saving");
  saving = api("/api/save?stem=" + encodeURIComponent(stem), { captions: caps })
    .then((r) => setStatus(`All changes saved (${r.count} captions, ${new Date().toLocaleTimeString()})`, "saved"))
    .catch((e) => { dirty = true; setStatus("Could not save: " + e.message + ". Is the tool still running?", "error"); })
    .finally(() => { saving = null; });
  await saving;
}

$("save").onclick = save;
$("lecture").onchange = (e) => load(e.target.value);
$("finish").onclick = async () => {
  await save();
  if (dirty) return;                                  // save failed; message already shown
  await api("/api/done", {}).catch(() => {});
  video.pause();
  $("main").innerHTML = '<div class="done" style="grid-column:1/-1"><h2>Review finished</h2>' +
    '<p class="help">Your captions are saved. You can close this tab and go back to the Lecture Editor.</p></div>';
  document.querySelector("header").querySelectorAll("button, select").forEach(b => b.disabled = true);
  setStatus("Saved and finished.", "saved");
};

$("findText").addEventListener("input", () => {
  const n = $("findText").value.trim().toLowerCase();
  const hits = n ? caps.filter(c => c.text.toLowerCase().includes(n)).length : 0;
  $("findInfo").textContent = n ? `${hits} caption(s) contain "${$("findText").value.trim()}".` : "";
  render();
});
$("replaceAll").onclick = () => {
  const find = $("findText").value;
  if (!find.trim()) return;
  const re = new RegExp(find.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  let n = 0;
  for (const c of caps) {
    const next = c.text.replace(re, () => { n++; return $("replaceText").value; });
    c.text = next;
  }
  $("findInfo").textContent = `Replaced ${n} time(s).`;
  render(); if (n) markDirty();
};

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && (e.key === "s" || e.key === "S")) { e.preventDefault(); save(); }
  if (e.ctrlKey && e.code === "Space") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
});
window.addEventListener("beforeunload", (e) => { if (dirty) { save(); e.preventDefault(); e.returnValue = ""; } });

init().catch((e) => setStatus("Could not load: " + e.message, "error"));
</script>
</body>
</html>
"""
