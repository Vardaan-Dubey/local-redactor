"""Optional local transcription with word timings (faster-whisper, MIT licensed,
open-weights Whisper models). Runs entirely on this machine.

The model is loaded with ``local_files_only=True`` so nothing is fetched at run
time. Fetch a model once with ``./setup.sh`` (or ``python -m redactor
download-model small``), or point ``--whisper-model`` at a local directory.
Audio never leaves the machine either way - only model weights are ever
downloaded, and only by that explicit command.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

Progress = Callable[[float, str], None]


def model_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "models" / "whisper"


def download_model(name: str = "small") -> Path:
    """The only network operation in this project, run explicitly by the user."""
    from faster_whisper.utils import download_model as dl
    out = model_dir() / name
    out.mkdir(parents=True, exist_ok=True)
    dl(name, output_dir=str(out))
    return out


def resolve_model(name_or_path: str) -> str:
    p = Path(name_or_path)
    if p.is_dir():
        return str(p)
    local = model_dir() / name_or_path
    if local.is_dir():
        return str(local)
    raise FileNotFoundError(
        f"Whisper model '{name_or_path}' not found locally. Run:\n"
        f"  python -m redactor download-model {name_or_path}\n"
        f"or pass a path to a local CTranslate2 Whisper model directory.")


def load_audio(media: str, sr: int = 16000):
    """Decode the first audio track to 16 kHz mono float32 with the bundled
    ffmpeg. Whisper gets raw samples, so faster-whisper's own PyAV decoder is
    never used (PyAV >= 17 breaks faster-whisper 1.2: 'metadata_errors')."""
    import numpy as np
    from .media import ffmpeg_exe
    p = subprocess.run([ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(media), "-map", "0:a:0",
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("Could not read audio from the media file:\n"
                           + p.stderr.decode(errors="replace")[-800:])
    return np.frombuffer(p.stdout, np.float32).copy()


def transcribe(media: str, out_json: str | Path, model: str = "small",
               language: str | None = None, progress: Progress = lambda f, m: None) -> Path:
    os.environ["HF_HUB_OFFLINE"] = "1"  # belt and braces: no hub calls
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("faster-whisper is not installed: pip install faster-whisper") from e

    path = resolve_model(model)
    progress(0.02, f"Loading local model {Path(path).name}")
    wm = WhisperModel(path, device="auto", compute_type="int8", local_files_only=True)
    progress(0.03, "Decoding audio")
    audio = load_audio(media)
    segs, info = wm.transcribe(audio, word_timestamps=True, language=language, vad_filter=True)
    out = []
    for s in segs:
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                    "words": [{"word": w.word.strip(), "start": w.start, "end": w.end}
                              for w in (s.words or [])]})
        if info.duration:
            progress(min(0.99, s.end / info.duration), f"Transcribing… {s.end:.0f}s / {info.duration:.0f}s")
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({"language": info.language, "segments": out}, indent=2,
                                   ensure_ascii=False), encoding="utf-8")
    progress(1.0, "Transcription done")
    return out_json
