"""Transcript loading, phrase search and redacted-copy writing.

Supported inputs:
  * .json  - Whisper / faster-whisper / WhisperX style output
             ({"segments": [{"start", "end", "text", "words": [...], "speaker"?}]})
             or this tool's own transcript JSON.
  * .srt   - SubRip subtitles
  * .vtt   - WebVTT (``<v Speaker>`` voice tags are kept as speaker labels)
  * .txt   - plain text (no timings: text redaction only)

When a cue/segment has no per-word timings, word times are interpolated across
the cue proportionally to word length. Precise per-word timings (from the
local Whisper transcription in ``transcribe.py``) give tighter mutes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REDACTED = "[REDACTED]"


@dataclass
class Word:
    text: str
    start: float | None = None
    end: float | None = None


@dataclass
class Segment:
    words: list[Word]
    start: float | None = None
    end: float | None = None
    speaker: str | None = None


@dataclass
class Transcript:
    fmt: str
    segments: list[Segment] = field(default_factory=list)
    source_path: str | None = None

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    @property
    def timed(self) -> bool:
        ws = self.words
        return bool(ws) and all(w.start is not None and w.end is not None for w in ws)

    def to_ui(self) -> dict:
        """Shape sent to the browser editor."""
        segs, idx = [], 0
        for s in self.segments:
            ws = []
            for w in s.words:
                ws.append({"i": idx, "t": w.text, "s": w.start, "e": w.end})
                idx += 1
            segs.append({"speaker": s.speaker, "start": s.start, "end": s.end, "words": ws})
        return {"format": self.fmt, "timed": self.timed, "segments": segs, "word_count": idx}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")
_TAG = re.compile(r"<[^>]+>")
_VOICE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")


def _ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"Bad timestamp: {s!r}")
    h, mnt, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000


def _interpolate(text: str, start: float | None, end: float | None) -> list[Word]:
    toks = text.split()
    if not toks:
        return []
    if start is None or end is None or end <= start:
        return [Word(t, start, end) for t in toks]
    weights = [len(t) + 1 for t in toks]
    total = sum(weights)
    out, t = [], start
    for tok, wgt in zip(toks, weights):
        dur = (end - start) * wgt / total
        out.append(Word(tok, round(t, 3), round(t + dur, 3)))
        t += dur
    return out


def _parse_cues(raw: str, fmt: str) -> Transcript:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    tr = Transcript(fmt=fmt)
    for block in re.split(r"\n\s*\n", raw):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        head = lines[0].strip()
        if fmt == "vtt" and (head.startswith("WEBVTT") or head.startswith("NOTE")
                             or head.startswith("STYLE") or head.startswith("REGION")):
            continue
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, b = lines[ti].split("-->", 1)
        start, end = _ts(a), _ts(b.strip().split()[0])
        text = " ".join(lines[ti + 1:])
        speaker = None
        vm = _VOICE.search(text)
        if vm:
            speaker = vm.group(1).strip()
        text = _TAG.sub("", text)
        text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&nbsp;", " "))
        words = _interpolate(text, start, end)
        if words:
            tr.segments.append(Segment(words, start, end, speaker))
    return tr


def _parse_json(raw: str) -> Transcript:
    data = json.loads(raw)
    segs = data.get("segments", data) if isinstance(data, dict) else data
    if not isinstance(segs, list):
        raise ValueError("JSON transcript must contain a 'segments' list")
    tr = Transcript(fmt="json")
    for s in segs:
        start, end = s.get("start"), s.get("end")
        speaker = s.get("speaker")
        words: list[Word] = []
        if s.get("words"):
            for w in s["words"]:
                txt = (w.get("word") if "word" in w else w.get("text", "")) or ""
                txt = txt.strip()
                if not txt:
                    continue
                words.append(Word(txt, w.get("start"), w.get("end")))
                speaker = speaker or w.get("speaker")
            # Fill any word missing timing from the segment bounds.
            for w in words:
                if w.start is None:
                    w.start = start
                if w.end is None:
                    w.end = end
        else:
            words = _interpolate(s.get("text", ""), start, end)
        if words:
            tr.segments.append(Segment(words, start, end, speaker))
    return tr


def _parse_txt(raw: str) -> Transcript:
    tr = Transcript(fmt="txt")
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line.strip():
            tr.segments.append(Segment([Word(t) for t in line.split()]))
    return tr


def load_transcript(path: str | Path) -> Transcript:
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig", errors="replace")  # read-only access
    ext = p.suffix.lower()
    if ext == ".json":
        tr = _parse_json(raw)
    elif ext == ".srt":
        tr = _parse_cues(raw, "srt")
    elif ext == ".vtt":
        tr = _parse_cues(raw, "vtt")
    else:
        tr = _parse_txt(raw)
    tr.source_path = str(p)
    if not tr.segments:
        raise ValueError(f"No transcript text found in {p}")
    return tr


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def _norm(t: str) -> str:
    return re.sub(r"[^\w']+", "", t.lower())


def find_phrase(tr: Transcript, phrase: str) -> list[int]:
    """Indices of every word that is part of an occurrence of ``phrase``
    (case/punctuation-insensitive, matched on whole words)."""
    target = [_norm(t) for t in phrase.split() if _norm(t)]
    if not target:
        return []
    words = [_norm(w.text) for w in tr.words]
    hits: list[int] = []
    n = len(target)
    for i in range(len(words) - n + 1):
        if words[i:i + n] == target:
            hits.extend(range(i, i + n))
    return sorted(set(hits))


# --------------------------------------------------------------------------- #
# Writing redacted copies (built from scratch - no original fields are copied,
# so un-redacted text cannot leak through e.g. Whisper's top-level "text").
# --------------------------------------------------------------------------- #

def _redacted_segments(tr: Transcript, redacted: set[int]):
    idx = 0
    for s in tr.segments:
        out_words: list[Word] = []
        prev_red = False
        for w in s.words:
            if idx in redacted:
                if prev_red:  # extend the previous [REDACTED] token
                    out_words[-1].end = w.end
                else:
                    out_words.append(Word(REDACTED, w.start, w.end))
                prev_red = True
            else:
                out_words.append(Word(w.text, w.start, w.end))
                prev_red = False
            idx += 1
        yield s, out_words


def _fmt_ts(t: float, sep: str) -> str:
    t = max(0.0, t or 0.0)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def render_redacted(tr: Transcript, redacted: Iterable[int], fmt: str | None = None) -> str:
    fmt = fmt or tr.fmt
    red = set(redacted)
    segs = list(_redacted_segments(tr, red))

    if fmt == "json":
        out = []
        for s, ws in segs:
            out.append({
                "start": s.start, "end": s.end, "speaker": s.speaker,
                "text": " ".join(w.text for w in ws),
                "words": [{"word": w.text, "start": w.start, "end": w.end} for w in ws],
            })
        return json.dumps({"segments": out}, indent=2, ensure_ascii=False) + "\n"

    if fmt in ("srt", "vtt"):
        lines = ["WEBVTT", ""] if fmt == "vtt" else []
        sep = "." if fmt == "vtt" else ","
        for n, (s, ws) in enumerate(segs, 1):
            start = s.start if s.start is not None else (ws[0].start or 0)
            end = s.end if s.end is not None else (ws[-1].end or start)
            text = " ".join(w.text for w in ws)
            if s.speaker:
                text = f"<v {s.speaker}>{text}" if fmt == "vtt" else f"{s.speaker}: {text}"
            if fmt == "srt":
                lines.append(str(n))
            lines.append(f"{_fmt_ts(start, sep)} --> {_fmt_ts(end, sep)}")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    # plain text
    out_lines = []
    for s, ws in segs:
        prefix = ""
        if s.start is not None:
            prefix += f"[{_fmt_ts(s.start, '.')[:-4]}] "
        if s.speaker:
            prefix += f"{s.speaker}: "
        out_lines.append(prefix + " ".join(w.text for w in ws))
    return "\n".join(out_lines) + "\n"
