"""Entry point.

  python -m redactor [--media FILE] [--transcript FILE]      # open the editor
  python -m redactor redact --media F --transcript T --find "Jane Doe" --find "555-0100"
  python -m redactor transcribe --media F [--model small]
  python -m redactor download-model small                     # one-time, explicit
  python -m redactor setup [--whisper-model small]            # fetch missing models (start scripts run this)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_YUNET = ROOT / "models" / "face_detection_yunet_2023mar.onnx"


YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")


def setup_models(whisper: str = "small") -> None:
    """Download whatever models are missing. Idempotent: does nothing (and uses
    no network) once both models are present. Works the same on every OS."""
    DEFAULT_YUNET.parent.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_YUNET.is_file() or DEFAULT_YUNET.stat().st_size < 10_000:
        print("==> Downloading YuNet face detector (OpenCV Zoo, MIT)")
        import urllib.request
        tmp = DEFAULT_YUNET.with_suffix(".part")
        try:
            urllib.request.urlretrieve(YUNET_URL, tmp)
            if tmp.stat().st_size < 10_000:
                raise RuntimeError("download too small")
            tmp.replace(DEFAULT_YUNET)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"   YuNet download failed ({e}); the built-in Haar face detector will be used.")
    from .transcribe import model_dir
    if not (model_dir() / whisper / "model.bin").is_file():
        print(f"==> Downloading Whisper model '{whisper}' for local transcription (one time)")
        try:
            from .transcribe import download_model
            download_model(whisper)
        except Exception as e:
            print(f"   Whisper download failed ({e}). You can still redact with an existing "
                  "transcript; launch again later to retry.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv and argv[0] in ("serve", "redact", "transcribe", "download-model", "setup") else "serve"
    ap = argparse.ArgumentParser(prog="python -m redactor " + cmd)
    if cmd == "setup":
        ap.add_argument("--whisper-model", default="small")
        setup_models(ap.parse_args(argv).whisper_model)
        return
    if cmd == "download-model":
        ap.add_argument("name", nargs="?", default="small")
        a = ap.parse_args(argv)
        from .transcribe import download_model
        print("Downloaded to", download_model(a.name))
        return

    ap.add_argument("--media")
    ap.add_argument("--transcript")
    ap.add_argument("--out", help="output root folder (default: <source>_redacted next to the source)")
    ap.add_argument("--whisper-model", default="small", help="model name in ./models/whisper or a path")
    ap.add_argument("--yunet-model", default=str(DEFAULT_YUNET),
                    help="YuNet face model (falls back to OpenCV Haar cascades if missing)")
    if cmd == "serve":
        ap.add_argument("--port", type=int, default=8765)
        ap.add_argument("--no-browser", action="store_true")
    if cmd == "redact":
        ap.add_argument("--find", action="append", default=[], help="phrase to redact (repeatable)")
        ap.add_argument("--indices", help="JSON file with a list of word indices to redact")
        ap.add_argument("--blur-mode", choices=["speaker", "faces", "frame"], default="speaker",
                        help="speaker: largest face only; faces: every face; frame: whole frame")
        ap.add_argument("--no-failsafe", action="store_true")
        ap.add_argument("--pick", action="append", default=[], metavar="T,X,Y",
                        help="speaker face: time in seconds and position as 0-1 fractions of "
                             "the frame, e.g. 41.5,0.27,0.30 (repeatable; speaker mode)")
    if cmd == "transcribe":
        ap.add_argument("--language")
    a = ap.parse_args(argv)
    cfg = {"whisper_model": a.whisper_model, "yunet_model": a.yunet_model}

    if cmd == "serve":
        from .server import serve
        serve(cfg, a.media, a.transcript, a.out, a.port, not a.no_browser)
    elif cmd == "transcribe":
        from .export import default_output_root
        from .transcribe import transcribe
        out = Path(a.out) if a.out else default_output_root(a.media, None)
        p = transcribe(a.media, out / "workspace" / (Path(a.media).stem + ".transcript.json"),
                       a.whisper_model, a.language,
                       lambda f, m: print(f"\r{f*100:5.1f}% {m}", end="", flush=True))
        print("\nWrote", p)
    elif cmd == "redact":
        from .export import default_output_root, export
        from .transcript import find_phrase, load_transcript
        tr = load_transcript(a.transcript)
        idx = set()
        for ph in a.find:
            hits = find_phrase(tr, ph)
            print(f"'{ph}': {len(hits)} word(s)")
            idx.update(hits)
        if a.indices:
            idx.update(json.loads(Path(a.indices).read_text()))
        res = export(tr, sorted(idx), a.media, a.out or default_output_root(a.media, a.transcript),
                     {"blur_mode": a.blur_mode, "failsafe": not a.no_failsafe,
                      "speaker_picks": [dict(zip("txy", map(float, p.split(",")))) for p in a.pick],
                      "yunet_model": a.yunet_model},
                     lambda f, m: print(f"\r{f*100:5.1f}% {m:60.60}", end="", flush=True))
        print("\n" + json.dumps(res["outputs"], indent=2))


if __name__ == "__main__":
    main()
