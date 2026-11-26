"""Localhost-only web server for the redaction editor (Python stdlib only).

* Binds to 127.0.0.1 - not reachable from the network.
* Rejects requests whose Host/Origin is not this localhost address
  (blocks DNS-rebinding / cross-site requests from other browser tabs).
* Strict Content-Security-Policy: the page can load nothing from the internet
  and can only talk back to this server.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import media as M
from .export import default_output_root, export
from .transcript import find_phrase, load_transcript

STATIC = Path(__file__).resolve().parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


class State:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.media: str | None = None
        self.media_info: dict | None = None
        self.transcript = None
        self.out_root: Path | None = None
        self.last_export: dict | None = None  # what the latest export of these files redacted
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def open(self, media: str | None, transcript: str | None, out_root: str | None = None):
        media = str(Path(media).expanduser().resolve()) if media else None
        transcript = str(Path(transcript).expanduser().resolve()) if transcript else None
        if media and not Path(media).is_file():
            raise FileNotFoundError(f"Media not found: {media}")
        if transcript and not Path(transcript).is_file():
            raise FileNotFoundError(f"Transcript not found: {transcript}")
        if not media and not transcript:
            raise ValueError("Provide a media file and/or a transcript")
        old_transcript = self.transcript.source_path if self.transcript else None
        if media != self.media or transcript != old_transcript:
            # An earlier export belongs to the previous files.
            self.cfg.pop("last_output_media", None)
            self.last_export = None
        self.media = media
        self.media_info = M.probe(media).to_dict() if media else None
        self.transcript = load_transcript(transcript) if transcript else None
        self.out_root = Path(out_root).expanduser().resolve() if out_root else \
            default_output_root(media, transcript)

    def snapshot(self) -> dict:
        return {
            "media": self.media, "media_info": self.media_info,
            "transcript_path": self.transcript.source_path if self.transcript else None,
            "transcript": self.transcript.to_ui() if self.transcript else None,
            "out_root": str(self.out_root) if self.out_root else None,
            "whisper_model": self.cfg.get("whisper_model"),
            "last_export": self.last_export,
        }

    def start_job(self, fn) -> str:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "status": "running", "progress": 0.0, "message": "Starting",
               "result": None, "error": None}
        self.jobs[jid] = job

        def progress(f, m):
            job["progress"], job["message"] = round(float(f), 4), m

        def run():
            try:
                job["result"] = fn(progress)
                job["status"] = "done"
            except Exception as e:  # surfaced to the UI
                traceback.print_exc()
                job["status"], job["error"] = "error", str(e)

        threading.Thread(target=run, daemon=True).start()
        return jid


def make_handler(state: State, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class H(BaseHTTPRequestHandler):
        server_version = "LocalRedactor"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep request logs quiet (paths can be sensitive)
            pass

        # ---------- helpers ----------
        def _hdrs(self, code, ctype, length=None, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self._hdrs(code, "application/json", len(body))
            self.wfile.write(body)

        def _ok_host(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json({"error": "forbidden host"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                self._json({"error": "forbidden origin"}, 403)
                return False
            return True

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # ---------- GET ----------
        def do_GET(self):
            if not self._ok_host():
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/state":
                return self._json(state.snapshot())
            if path.startswith("/api/job/"):
                job = state.jobs.get(path.rsplit("/", 1)[-1])
                return self._json(job or {"error": "no such job"}, 200 if job else 404)
            if path == "/media":
                return self._media(state.media)
            if path == "/output-media":
                return self._media(state.cfg.get("last_output_media"))
            self._json({"error": "not found"}, 404)

        def _static(self, name):
            p = (STATIC / name).resolve()
            if STATIC not in p.parents or not p.is_file():
                return self._json({"error": "not found"}, 404)
            data = p.read_bytes()
            self._hdrs(200, mimetypes.guess_type(p.name)[0] or "application/octet-stream", len(data))
            self.wfile.write(data)

        def _media(self, fpath):
            """Read-only streaming with HTTP Range support (for seeking)."""
            if not fpath or not Path(fpath).is_file():
                return self._json({"error": "no media"}, 404)
            size = os.path.getsize(fpath)
            ctype = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end, code = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self._hdrs(416, ctype, 0, {"Content-Range": f"bytes */{size}"})
                    return
                code = 206
            extra = {"Accept-Ranges": "bytes"}
            if code == 206:
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            self._hdrs(code, ctype, end - start + 1, extra)
            try:
                with open(fpath, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---------- POST ----------
        def do_POST(self):
            if not self._ok_host():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json({"error": "expected JSON"}, 415)
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/open":
                    state.open(body.get("media") or None, body.get("transcript") or None,
                               body.get("out_root") or None)
                    return self._json(state.snapshot())
                if path == "/api/find":
                    if not state.transcript:
                        raise ValueError("No transcript loaded")
                    return self._json({"indices": find_phrase(state.transcript, body.get("query", ""))})
                if path == "/api/export":
                    return self._export(body)
                if path == "/api/transcribe":
                    return self._transcribe(body)
                if path == "/api/pick":
                    from .picker import pick
                    return self._json({"path": pick(body.get("kind", ""), body.get("start") or None)})
                self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 400)

        def _export(self, body):
            if not state.transcript:
                raise ValueError("No transcript loaded")
            tr, media, out_root = state.transcript, state.media, state.out_root
            opts = dict(body.get("options") or {})
            opts["yunet_model"] = state.cfg.get("yunet_model")

            def job(progress):
                res = export(tr, body.get("redacted", []), media, out_root, opts, progress)
                if state.media == media and state.transcript is tr:  # files not switched meanwhile
                    state.cfg["last_output_media"] = res["outputs"].get("media")
                    state.last_export = {"redacted": sorted(body.get("redacted", [])),
                                         "options": body.get("options") or {},
                                         "has_media": bool(res["outputs"].get("media"))}
                return res

            self._json({"job": state.start_job(job)})

        def _transcribe(self, body):
            if not state.media:
                raise ValueError("Open a media file first")
            from .transcribe import transcribe
            media, out_root = state.media, state.out_root
            model = body.get("model") or state.cfg.get("whisper_model", "small")
            lang = body.get("language") or None

            def job(progress):
                out = transcribe(media, out_root / "workspace" / (Path(media).stem + ".transcript.json"),
                                 model, lang, progress)
                state.open(media, str(out), str(out_root))
                return {"transcript": str(out)}

            self._json({"job": state.start_job(job)})

    return H


def serve(cfg: dict, media=None, transcript=None, out_root=None, port=8765, open_browser=True):
    state = State(cfg)
    if media or transcript:
        state.open(media, transcript, out_root)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"Local Redactor running at {url}  (localhost only - Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
