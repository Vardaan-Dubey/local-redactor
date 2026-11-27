"""Produce redacted *copies* of the transcript and media.

Guarantees:
  * Source files are only opened for reading.
  * Outputs go to a fresh, timestamped folder; existing files are never overwritten.
  * Any output path that resolves to a source path is refused.
  * SHA-256 of every source is taken before and after; a mismatch fails the export.
  * The report never contains the redacted words themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable

from . import media as M
from .transcript import Transcript, render_redacted

Progress = Callable[[float, str], None]


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:  # read-only
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _guard(out: Path, sources: list[Path]) -> None:
    ro = out.resolve()
    for s in sources:
        if ro == s.resolve():
            raise RuntimeError(f"Refusing to write over source file: {s}")


def default_output_root(media: str | None, transcript: str | None) -> Path:
    ref = Path(media or transcript)
    return ref.parent / f"{ref.stem}_redacted"


DEFAULTS = {"pad_before": 0.10, "pad_after": 0.15, "merge_gap": 0.25,
            "blur_mode": "speaker", "failsafe": True, "blur_pad": 0.25,
            "detect_every": 1, "yunet_model": None}


def merged_options(options: dict | None) -> dict:
    opts = dict(DEFAULTS)
    opts.update({k: v for k, v in (options or {}).items() if v is not None})
    return opts


def redaction_spans(transcript: Transcript, red: list[int], duration: float, opts: dict):
    """(mute spans, blur spans) in seconds for the redacted word indices."""
    words = transcript.words
    times = [(words[i].start, words[i].end) for i in red]
    spans = M.compute_spans(times, opts["pad_before"], opts["pad_after"],
                            opts["merge_gap"], duration)
    blur_spans = M.compute_spans(times, opts["pad_before"] + opts["blur_pad"],
                                 opts["pad_after"] + opts["blur_pad"],
                                 opts["merge_gap"], duration)
    return spans, blur_spans


def preview(transcript: Transcript, redacted: list[int], media_path: str, options: dict,
            progress: Progress = lambda f, m: None, cancelled=lambda: False) -> dict:
    """What an export with these marks and settings would mute and blur, without
    writing anything: mute spans, blur spans and the per-frame blur regions."""
    opts = merged_options(options)
    if not transcript.timed:
        raise RuntimeError("Transcript has no timings, so media cannot be aligned.")
    red = sorted(i for i in set(int(i) for i in redacted) if 0 <= i < len(transcript.words))
    info = M.probe(media_path)
    spans, blur_spans = redaction_spans(transcript, red, info.duration, opts)
    frames = []
    if info.has_video and blur_spans:
        blurrer = M.FaceBlurrer(opts["blur_mode"], bool(opts["failsafe"]),
                                detect_every=int(opts["detect_every"]),
                                yunet_model=opts["yunet_model"], picks=opts.get("speaker_picks"))
        frames = M.preview_regions(media_path, info, blur_spans, blurrer, progress, cancelled)
    return {"spans": spans, "blur_spans": blur_spans, "fps": float(info.fps), "frames": frames}


def export(transcript: Transcript, redacted: list[int], media_path: str | None,
           out_root: str | Path, options: dict, progress: Progress = lambda f, m: None) -> dict:
    opts = merged_options(options)

    sources = [Path(transcript.source_path)] + ([Path(media_path)] if media_path else [])
    for s in sources:
        if not s.is_file():
            raise FileNotFoundError(s)

    progress(0.0, "Hashing originals")
    before = {str(s): sha256(s) for s in sources}

    out_dir = _unique(Path(out_root) / time.strftime("export_%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=False)

    red = sorted(set(int(i) for i in redacted))
    words = transcript.words
    red = [i for i in red if 0 <= i < len(words)]
    outputs: dict[str, str] = {}

    # ---- transcript copy (same format) + readable .txt -----------------------
    tsrc = Path(transcript.source_path)
    t_out = _unique(out_dir / f"{tsrc.stem}.redacted{tsrc.suffix or '.txt'}")
    _guard(t_out, sources)
    t_out.write_text(render_redacted(transcript, red), encoding="utf-8")
    outputs["transcript"] = str(t_out)
    if transcript.fmt != "txt":
        txt_out = _unique(out_dir / f"{tsrc.stem}.redacted.txt")
        txt_out.write_text(render_redacted(transcript, red, "txt"), encoding="utf-8")
        outputs["transcript_txt"] = str(txt_out)

    # ---- media --------------------------------------------------------------
    spans: list[tuple[float, float]] = []
    stats = {}
    info = None
    if media_path:
        if red and not transcript.timed:
            raise RuntimeError("Transcript has no timings, so media cannot be aligned. "
                               "Use an SRT/VTT/JSON transcript or run local transcription.")
        info = M.probe(media_path)
        spans, blur_spans = redaction_spans(transcript, red, info.duration, opts)
        src = Path(media_path)
        sub = lambda f, m: progress(0.05 + 0.9 * f, m)
        if info.has_video:
            m_out = _unique(out_dir / f"{src.stem}.redacted.mp4")
            _guard(m_out, sources)
            blurrer = M.FaceBlurrer(opts["blur_mode"], bool(opts["failsafe"]),
                                    detect_every=int(opts["detect_every"]),
                                    yunet_model=opts["yunet_model"],
                                    picks=opts.get("speaker_picks"))
            M.redact_video(str(src), str(m_out), info, spans, blur_spans, blurrer, sub)
            stats = dict(blurrer.stats, detector=getattr(blurrer.detector, "kind", "none"))
        elif info.has_audio:
            ext = src.suffix.lower() if src.suffix.lower() in M.AUDIO_CODECS else ".wav"
            m_out = _unique(out_dir / f"{src.stem}.redacted{ext}")
            _guard(m_out, sources)
            M.redact_audio_only(str(src), str(m_out), spans, sub)
        else:
            raise RuntimeError("Media file has no audio or video streams")
        outputs["media"] = str(m_out)

    # ---- integrity check ------------------------------------------------------
    progress(0.97, "Verifying originals are unchanged")
    after = {str(s): sha256(s) for s in sources}
    if after != before:
        raise RuntimeError("INTEGRITY FAILURE: a source file changed during export!")

    report = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "redacted_word_count": len(red),
        "redacted_word_indices": red,  # positions only - never the words themselves
        "muted_and_blurred_spans_sec": spans,
        "options": {k: v for k, v in opts.items() if k != "yunet_model"},
        "video_blur_stats": stats,
        "sources_sha256_unchanged": before,
        "outputs_sha256": {k: sha256(v) for k, v in outputs.items()},
    }
    rep = out_dir / "redaction_report.json"
    rep.write_text(json.dumps(report, indent=2), encoding="utf-8")
    outputs["report"] = str(rep)
    progress(1.0, "Done")
    return {"out_dir": str(out_dir), "outputs": outputs, "spans": spans, "stats": stats}
