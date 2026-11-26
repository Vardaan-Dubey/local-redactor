"""End-to-end test with synthetic media: python -m unittest discover tests"""
import json, re, subprocess, tempfile, unittest
from pathlib import Path

from redactor.export import export, sha256
from redactor.media import ffmpeg_exe
from redactor.transcript import find_phrase, load_transcript

SRT = """1
00:00:00,500 --> 00:00:03,000
Hello my name is Jane Doe and

2
00:00:03,000 --> 00:00:06,000
my phone number is 555 0100 thanks
"""


def max_db(path, start, dur):
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-ss", str(start), "-t", str(dur), "-i", str(path),
                          "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(re.search(r"max_volume: (-?[\d.]+)", out).group(1))


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.mp4, self.srt = self.d / "src.mp4", self.d / "src.srt"
        subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=7",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=7", "-metadata", "title=secret",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.mp4)], check=True)
        self.srt.write_text(SRT)

    def test_redact_video(self):
        tr = load_transcript(self.srt)
        idx = find_phrase(tr, "jane doe") + find_phrase(tr, "555 0100")
        self.assertEqual(len(idx), 4)
        before = (sha256(self.mp4), sha256(self.srt))
        res = export(tr, idx, str(self.mp4), self.d / "out", {})
        self.assertEqual(before, (sha256(self.mp4), sha256(self.srt)))   # originals untouched

        text = Path(res["outputs"]["transcript"]).read_text()
        self.assertNotIn("Jane", text); self.assertNotIn("555", text); self.assertIn("[REDACTED]", text)
        self.assertNotIn("Jane", Path(res["outputs"]["report"]).read_text())

        out = res["outputs"]["media"]
        for s, e in res["spans"]:
            self.assertLess(max_db(out, s + 0.05, e - s - 0.1), -80)   # muted
        self.assertGreater(max_db(out, 0.0, 1.0), -40)                  # untouched audio audible
        tags = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", out],
                              capture_output=True, text=True).stderr
        self.assertNotIn("secret", tags)                                 # metadata stripped
        self.assertGreater(res["stats"]["frames"], 0)                    # frames were blurred


if __name__ == "__main__":
    unittest.main()
