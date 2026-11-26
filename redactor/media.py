"""Audio muting and speaker (face) blurring.

Everything runs locally with ffmpeg + OpenCV. The source file is only ever
*read* (ffmpeg ``-i``); output is always written to a different path.

Video pipeline:
    ffmpeg (decode, constant frame rate, raw BGR frames)
        -> Python/OpenCV (blur faces on frames inside redaction spans)
        -> ffmpeg (encode H.264, mux original audio with mute filter applied)
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

Progress = Callable[[float, str], None]

AUDIO_CODECS = {
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
}


@functools.lru_cache(maxsize=None)
def ffmpeg_exe() -> str:
    """The ffmpeg binary to use, in order of preference:
    $REDACTOR_FFMPEG, an ffmpeg on PATH, or the static build bundled in the
    ``imageio-ffmpeg`` pip package (no Homebrew/apt/winget or admin rights needed)."""
    env = os.environ.get("REDACTOR_FFMPEG")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise RuntimeError("ffmpeg not found. Run setup.sh (installs a bundled ffmpeg via pip) "
                           "or set REDACTOR_FFMPEG=/path/to/ffmpeg.") from e


def require_ffmpeg() -> None:
    ffmpeg_exe()


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #

@dataclass
class MediaInfo:
    path: str
    duration: float
    has_audio: bool
    has_video: bool
    width: int = 0
    height: int = 0
    fps: Fraction = Fraction(0)

    def to_dict(self) -> dict:
        return {"path": self.path, "duration": self.duration, "has_audio": self.has_audio,
                "has_video": self.has_video, "width": self.width, "height": self.height,
                "fps": float(self.fps)}


def _fps_fraction(v: float) -> Fraction:
    r = round(v)
    if r and abs(v - r * 1000 / 1001) < 0.005:  # NTSC rates: 29.97 -> 30000/1001
        return Fraction(r * 1000, 1001)
    return Fraction(v).limit_denominator(1001)


def probe(path: str | Path) -> MediaInfo:
    """Stream info via ffprobe when it is on PATH, otherwise by parsing
    ``ffmpeg -i`` (the bundled pip ffmpeg ships without ffprobe)."""
    ffprobe = shutil.which("ffprobe")
    info = _probe_ffprobe(ffprobe, path) if ffprobe and not os.environ.get("REDACTOR_FFMPEG") \
        else _probe_ffmpeg(path)
    if info.has_video and (info.fps <= 0 or info.fps > 240):
        info.fps = Fraction(30)
    return info


def _probe_ffprobe(ffprobe: str, path) -> MediaInfo:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, check=True, text=True).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    # Ignore cover-art "video" streams in audio files.
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    info = MediaInfo(str(path), float(data.get("format", {}).get("duration") or 0),
                     bool(auds), bool(vids))
    if vids:
        v = vids[0]
        w, h = int(v["width"]), int(v["height"])
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        rot = rot or int(v.get("tags", {}).get("rotate", 0) or 0)
        if abs(rot) % 180 == 90:  # ffmpeg auto-rotates on decode
            w, h = h, w
        info.width, info.height = w, h
        rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        try:
            fps = Fraction(rate)
        except (ValueError, ZeroDivisionError):
            fps = Fraction(0)
        if fps <= 0 or fps > 240:
            fps = Fraction(v.get("r_frame_rate", "30/1")) if v.get("r_frame_rate") else Fraction(30)
        info.fps = fps.limit_denominator(1001)
        if not info.duration:
            info.duration = float(v.get("duration") or 0)
    return info


def _probe_ffmpeg(path) -> MediaInfo:
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)],
                         capture_output=True, text=True, errors="replace").stderr
    if "Input #0" not in err:
        raise RuntimeError(f"ffmpeg could not read {path}:\n{err[-800:]}")
    dur = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    streams: list[list] = []  # [stream line, [indented detail lines]]
    for ln in err.splitlines():
        s = ln.strip()
        if s.startswith("Stream #0:"):
            streams.append([s, []])
        elif streams and ln.startswith("    "):
            streams[-1][1].append(s)
        elif streams and not ln.startswith(" "):
            break
    vids = [st for st in streams if ": Video: " in st[0] and "(attached pic)" not in st[0]]
    auds = [st for st in streams if ": Audio: " in st[0]]
    info = MediaInfo(str(path), dur, bool(auds), bool(vids))
    if vids:
        line, extra = vids[0]
        m = re.search(r"[ ,](\d{2,5})x(\d{2,5})[ ,\]]", line + " ")
        if not m:
            raise RuntimeError(f"Could not read video size of {path}")
        w, h = int(m.group(1)), int(m.group(2))
        rot = 0
        for e in extra:
            rm = re.search(r"rotation of (-?[\d.]+) degrees", e) or re.match(r"rotate\s*:\s*(-?\d+)", e)
            if rm:
                rot = int(float(rm.group(1)))
        if abs(rot) % 180 == 90:
            w, h = h, w
        info.width, info.height = w, h
        fm = re.search(r", ([\d.]+) fps", line) or re.search(r", ([\d.]+) tbr", line)
        info.fps = _fps_fraction(float(fm.group(1))) if fm else Fraction(30)
    return info


# --------------------------------------------------------------------------- #
# Redaction spans
# --------------------------------------------------------------------------- #

def compute_spans(times: Iterable[tuple[float, float]], pad_before: float = 0.1,
                  pad_after: float = 0.15, merge_gap: float = 0.25,
                  duration: float | None = None) -> list[tuple[float, float]]:
    spans = sorted((max(0.0, s - pad_before), e + pad_after) for s, e in times)
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if duration:
        merged = [[s, min(e, duration)] for s, e in merged if s < duration]
    return [(round(s, 3), round(e, 3)) for s, e in merged]


def mute_filter(spans: list[tuple[float, float]]) -> str | None:
    if not spans:
        return None
    cond = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in spans)
    return f"volume=enable='{cond}':volume=0"


class _SpanLookup:
    def __init__(self, spans):
        self.spans = spans
        self.i = 0

    def __call__(self, t: float) -> bool:  # t is monotonically increasing
        while self.i < len(self.spans) and self.spans[self.i][1] < t:
            self.i += 1
        return self.i < len(self.spans) and self.spans[self.i][0] <= t <= self.spans[self.i][1]


# --------------------------------------------------------------------------- #
# Face detection / blurring
# --------------------------------------------------------------------------- #

class FaceDetector:
    """YuNet (if the ONNX model is present in ./models) else Haar cascades.
    Both ship with / are loaded by OpenCV - no network access, no cloud."""

    def __init__(self, yunet_model: str | None = None):
        import cv2
        self.cv2 = cv2
        self.kind = "haar"
        if yunet_model and Path(yunet_model).is_file() and hasattr(cv2, "FaceDetectorYN"):
            self.yunet = cv2.FaceDetectorYN.create(yunet_model, "", (320, 320), 0.6, 0.3, 5000)
            self.kind = "yunet"
        else:
            base = Path(cv2.data.haarcascades)
            self.frontal = cv2.CascadeClassifier(str(base / "haarcascade_frontalface_default.xml"))
            self.profile = cv2.CascadeClassifier(str(base / "haarcascade_profileface.xml"))
            if self.frontal.empty():
                raise RuntimeError("OpenCV Haar cascades not found; install opencv-python-headless<5")

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        cv2 = self.cv2
        h, w = frame.shape[:2]
        scale = min(1.0, 640 / max(w, h))
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame
        sh, sw = small.shape[:2]
        boxes = []
        if self.kind == "yunet":
            self.yunet.setInputSize((sw, sh))
            _, faces = self.yunet.detect(small)
            if faces is not None:
                boxes = [tuple(f[:4]) for f in faces]
        else:
            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            mn = (max(20, sw // 40), max(20, sw // 40))
            for casc, flip in ((self.frontal, False), (self.profile, False), (self.profile, True)):
                img = cv2.flip(gray, 1) if flip else gray
                for (x, y, bw, bh) in casc.detectMultiScale(img, 1.1, 5, minSize=mn):
                    if flip:
                        x = sw - x - bw
                    boxes.append((x, y, bw, bh))
        return [(int(x / scale), int(y / scale), int(bw / scale), int(bh / scale))
                for x, y, bw, bh in boxes]


def _expand(box, w, h, fx=0.35, fy=0.5):
    x, y, bw, bh = box
    x0 = max(0, int(x - bw * fx)); y0 = max(0, int(y - bh * fy))
    x1 = min(w, int(x + bw * (1 + fx))); y1 = min(h, int(y + bh * (1 + fy * 0.6)))
    return x0, y0, x1, y1


def obscure(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> None:
    """Irreversibly obscure a region: heavy pixelation, then Gaussian blur."""
    import cv2
    roi = frame[y0:y1, x0:x1]
    rh, rw = roi.shape[:2]
    if rh < 2 or rw < 2:
        return
    small = cv2.resize(roi, (max(1, rw // 24), max(1, rh // 24)), interpolation=cv2.INTER_AREA)
    big = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    frame[y0:y1, x0:x1] = cv2.GaussianBlur(big, (0, 0), sigmaX=max(rw, rh) / 15)


class FaceBlurrer:
    """Blurs faces on frames inside redaction spans.

    mode "speaker": only one face. If the user picked the speaker in the editor
    (``picks``: [{"t": sec, "x": 0..1, "y": 0..1}]), the face nearest the active
    pick is tracked frame to frame; otherwise the largest face is used.
    mode "faces": every detected face. mode "frame": the whole frame.
    """

    def __init__(self, mode: str = "faces", failsafe: bool = True, hold: float = 0.75,
                 detect_every: int = 1, yunet_model: str | None = None, picks=None):
        if mode not in ("speaker", "faces", "frame"):
            raise ValueError(f"Unknown blur mode: {mode!r}")
        self.mode = mode
        self.failsafe = failsafe
        self.hold = hold
        self.detect_every = max(1, detect_every)
        self.detector = FaceDetector(yunet_model) if mode in ("faces", "speaker") else None
        self.picks = sorted(
            ({"t": float(p["t"]), "x": min(1.0, max(0.0, float(p["x"]))),
              "y": min(1.0, max(0.0, float(p["y"])))} for p in (picks or [])),
            key=lambda p: p["t"])
        self.recent: list[tuple[tuple, float]] = []
        self.speaker: tuple | None = None  # last box chosen as the speaker
        self.active_pick = None
        self.last_t: float | None = None
        self.speaker_t = 0.0
        self.n = 0
        self.n_obs = 0
        self.stats = {"frames": 0, "face_frames": 0, "fullframe_frames": 0}
        if self.picks:
            self.stats["speaker_picks"] = len(self.picks)
            self.stats["pick_misses"] = 0

    def __call__(self, frame: np.ndarray, t: float) -> None:
        """Called for frames inside a redaction span: obscure them."""
        h, w = frame.shape[:2]
        self.stats["frames"] += 1
        if self.mode == "frame":
            obscure(frame, 0, 0, w, h)
            self.stats["fullframe_frames"] += 1
            return
        if not self.picks and self.last_t is not None and t - self.last_t > 0.5:
            # New span with no user pick: the shot may have changed, start over.
            self.speaker, self.recent = None, []
        self.last_t = t
        if self.n % self.detect_every == 0:
            boxes = self.detector.detect(frame)
            if self.mode == "speaker":
                if self.picks:
                    one = self._track_pick(boxes, t, w, h)
                    if one is None:
                        self.stats["pick_misses"] += 1
                    boxes = [one] if one is not None else []
                elif boxes:
                    boxes = [self._pick_largest(boxes)]
            for b in boxes:
                self.recent.append((b, t))
        self.n += 1
        # Keep boxes seen within the hold window so blinks/turns don't un-blur.
        self.recent = [(b, ts) for b, ts in self.recent if t - ts <= self.hold]
        if not self.recent:
            if self.failsafe:  # speaker not found: never leave them exposed
                obscure(frame, 0, 0, w, h)
                self.stats["fullframe_frames"] += 1
            return
        for b, _ in self.recent:
            obscure(frame, *_expand(b, w, h))
        self.stats["face_frames"] += 1

    def observe(self, frame: np.ndarray, t: float) -> None:
        """Called for frames outside redaction spans (not modified). With a user
        pick, keep following the picked face between spans so a moving camera
        doesn't hand the blur to someone else. Detects on every 3rd frame."""
        self.last_t = t
        if self.mode != "speaker" or not self.picks:
            return
        if t < self.picks[0]["t"] - self.anchor_window:
            return
        self.n_obs += 1
        if self.n_obs % 3:
            return
        h, w = frame.shape[:2]
        self._track_pick(self.detector.detect(frame), t, w, h)

    anchor_window = 2.0  # a pick can anchor the face this many seconds either side

    def _pick_for(self, t: float):
        """The latest pick at or before t (else the first one)."""
        cur = self.picks[0]
        for p in self.picks:
            if p["t"] <= t:
                cur = p
            else:
                break
        return cur

    def _track_pick(self, boxes, t, w, h):
        pick = self._pick_for(t)
        if pick is not self.active_pick:  # a new pick (e.g. after a camera cut) wins
            self.active_pick, self.speaker = pick, None
        if self.speaker is not None and t - self.speaker_t > self.hold:
            self.speaker = None  # lost the face (cut, left the shot)
        centre = lambda b: (b[0] + b[2] / 2, b[1] + b[3] / 2)
        if self.speaker is None:
            # Only anchor on the click near the moment it was made: elsewhere the
            # same screen spot may be someone else. Then the failsafe blurs the frame.
            if abs(t - pick["t"]) > self.anchor_window:
                return None
            tx, ty = pick["x"] * w, pick["y"] * h
            limit = 0.12 * (w * w + h * h) ** 0.5
        else:
            tx, ty = centre(self.speaker)
            limit = 1.2 * max(self.speaker[2], self.speaker[3])
        best, bd = None, None
        for b in boxes:
            cx, cy = centre(b)
            d = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
            # A click anywhere on a face box always matches that face.
            inside = self.speaker is None and b[0] <= tx <= b[0] + b[2] and b[1] <= ty <= b[1] + b[3]
            if inside:
                d = 0.0
            if (inside or d <= limit) and (bd is None or d < bd):
                best, bd = b, d
        if best is not None:
            self.speaker, self.speaker_t = best, t
        return best

    def _pick_largest(self, boxes: list[tuple]) -> tuple:
        """No pick given: take the largest face (close-up or inset shot). Stick
        with the face chosen last time unless another is clearly bigger."""
        area = lambda b: b[2] * b[3]
        best = max(boxes, key=area)
        if self.speaker is not None:
            sx, sy, sw, sh = self.speaker
            cx, cy = sx + sw / 2, sy + sh / 2
            near = [b for b in boxes
                    if abs(b[0] + b[2] / 2 - cx) < sw and abs(b[1] + b[3] / 2 - cy) < sh]
            if near:
                prev = max(near, key=area)
                if area(prev) >= 0.6 * area(best):
                    best = prev
        self.speaker = best
        return best


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _run_checked(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{p.stderr[-2000:]}")


def redact_audio_only(src: str, dst: str, spans, progress: Progress) -> None:
    progress(0.1, "Muting audio")
    ext = Path(dst).suffix.lower()
    cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-y", "-i", src, "-map", "0:a",
           "-map_metadata", "-1", "-map_chapters", "-1", "-vn", "-sn", "-dn"]
    f = mute_filter(spans)
    if f:
        cmd += ["-af", f]
    cmd += AUDIO_CODECS.get(ext, []) + [dst]
    _run_checked(cmd)
    progress(1.0, "Audio done")


def redact_video(src: str, dst: str, info: MediaInfo, spans, blur_spans, blurrer: FaceBlurrer,
                 progress: Progress) -> None:
    w, h, fps = info.width, info.height, info.fps
    frame_bytes = w * h * 3
    total = max(1, int(info.duration * float(fps)))
    in_blur = _SpanLookup(blur_spans)

    dec = subprocess.Popen(
        [ffmpeg_exe(), "-nostdin", "-v", "error", "-i", src, "-map", "0:v:0", "-an", "-sn",
         "-r", f"{fps.numerator}/{fps.denominator}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    enc_err = tempfile.TemporaryFile()
    enc_cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
               "-r", f"{fps.numerator}/{fps.denominator}", "-i", "pipe:0",
               "-i", src, "-map", "0:v:0"]
    if info.has_audio:
        enc_cmd += ["-map", "1:a"]
        f = mute_filter(spans)
        if f:
            enc_cmd += ["-af", f]
        enc_cmd += ["-c:a", "aac", "-b:a", "192k"]
    # Drop subtitles/data/metadata/chapters: embedded captions or tags could
    # otherwise carry the un-redacted text into the copy.
    enc_cmd += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", dst]
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=enc_err)

    i = 0
    try:
        while True:
            buf = dec.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            t = i / float(fps)
            if in_blur(t):
                frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy()
                blurrer(frame, t)
                buf = frame.tobytes()
            else:
                blurrer.observe(np.frombuffer(buf, np.uint8).reshape(h, w, 3), t)
            enc.stdin.write(buf)
            i += 1
            if i % 30 == 0:
                progress(min(0.99, i / total), f"Processing video frame {i}/{total}")
    finally:
        enc.stdin.close()
        dec.stdout.close()
        dec.wait()
        enc.wait()
    if enc.returncode != 0:
        enc_err.seek(0)
        raise RuntimeError("ffmpeg encode failed:\n" + enc_err.read().decode(errors="replace")[-2000:])
    if i == 0:
        raise RuntimeError("No video frames could be decoded from the source.")
    progress(1.0, f"Video done ({i} frames)")
