# Local Redactor

Redact text, audio and video from a source transcript, **entirely on your own machine**.

Select text in a transcript editor. On export, the tool writes copies where:

- **text**: the selected words become `[REDACTED]` in a copy of the transcript (same format, plus a readable `.txt`)
- **audio**: the matching time ranges are muted
- **video**: faces are blurred during those time ranges (the whole frame is blurred if no face is found)

The originals are never modified.

## Privacy model

| Concern | How it's handled |
|---|---|
| Cloud / closed models | None. Transcription uses open-weight Whisper through `faster-whisper` (MIT). Face detection uses OpenCV (Apache-2.0). Muting and encoding use ffmpeg (a static build bundled through pip). |
| Network at run time | None. The Whisper model is loaded with `local_files_only=True` and `HF_HUB_OFFLINE=1`. The only network step is the one-time `setup.sh`, which installs packages and downloads models. After that you can run with the network disconnected. |
| Browser UI | The server binds to `127.0.0.1` only and rejects other Host/Origin headers. A strict Content-Security-Policy stops the page from loading or sending anything off-machine. There are no CDNs or external fonts. |
| Originals | Opened read-only. Outputs go to a new timestamped folder and never overwrite anything. Writing to a source path is refused. SHA-256 hashes of the sources are checked before and after each export, and a mismatch fails the export. |
| Leaks through side channels | Metadata, chapters and embedded subtitle tracks are dropped from the redacted media. Transcript copies are rebuilt from scratch, so Whisper's top-level `text` field can't carry content over. The report records only word positions and times, never the redacted words. Server request logging is off. |

## Start (macOS, Windows, Linux)

Unzip the folder anywhere, then:

| System | Do this |
|---|---|
| **macOS** | Double-click **`Start Redactor.command`**. If macOS says it can't verify the developer, open **System Settings → Privacy & Security** and click **Open Anyway**, or run `bash start.sh` in Terminal from this folder. |
| **Windows** | Double-click **`Start Redactor.bat`**. If SmartScreen warns, click **More info → Run anyway**. |
| **Linux** | Run `./start.sh` in a terminal. |

The **first start** sets everything up and needs internet once. It takes a few minutes and about 1.5 GB of disk:
- [uv](https://docs.astral.sh/uv/) (Python installer) into `~/.local/bin`
- a private Python 3.12 and the pinned packages into `.venv`, including a bundled ffmpeg
- the YuNet face model and the Whisper "small" model into `models/`

No admin rights, Homebrew, sudo or winget are needed, and nothing is installed system-wide. **Later starts run fully offline.** The editor opens in your browser at http://127.0.0.1:8765. Keep the terminal window open while you use it; closing it stops the editor.

You don't need to restart after an update. When the app's code changes (after a `git pull`, for example), the editor restarts itself within a second or two. **Refresh the page** and the same files reopen. Starting it a second time just opens the copy that's already running. To turn the automatic restart off, start it with `--no-reload`.

For more accurate transcription, set `WHISPER_MODEL=large-v3` before the first start (about 3 GB).

If you already have ffmpeg somewhere, `REDACTOR_FFMPEG=/path/to/ffmpeg` overrides the bundled one.

To remove everything, delete this folder (and `~/.local/bin/uv` if you don't use uv elsewhere).

## Use

```bash
./start.sh --media ~/Desktop/interview.mp4 --transcript ~/Desktop/interview.srt
```

This opens the editor in your browser. You can also start it with no arguments and use **Browse…** to choose files, or type or paste paths into the top bar. On a Mac, drag a file from Finder into Terminal or into the box to paste its path.

1. **Select text** with the mouse and press **R** (or click the floating *Redact* button). Press **U** to un-redact a selection. **Ctrl/Cmd+Z** undoes.
2. **Find phrase → Redact all matches** redacts every occurrence of a name, number and so on.
3. Click any word to jump the player there. Use the **Original / Redacted** switch under the player (or press **V**) to compare. *Original* plays the source with redacted words readable but marked. *Redacted* plays the exported copy whose report matches your current marks and settings. If no export matches yet, it shows a rough preview: the words are blacked out and muted, and the whole frame is blurred while they're spoken, because the browser can't find faces. Opening a `.redacted` file the tool made plays it as is.
4. **Export redacted copies**. The *Redacted* view then plays the exported file.

Your selections are saved automatically in the browser's local storage. Only word positions and the paths of the open files are saved, not the words.

No transcript yet? Enter the media path and click **Transcribe locally**. This produces a transcript with timings for each word, which gives tighter mutes than SRT/VTT. That transcript is unredacted and is saved in `<output>/workspace/`.

### Output

```
<source>_redacted/export_YYYYMMDD_HHMMSS/
  interview.redacted.mp4      # H.264/AAC, muted + blurred, metadata stripped
  interview.redacted.srt      # same format as the input transcript
  interview.redacted.txt
  redaction_report.json       # spans, settings, source hashes (verified unchanged), output hashes
```

### Headless / batch

```bash
./start.sh redact --media call.wav --transcript call.json --find "Jane Doe" --find "555 0100"
./start.sh transcribe --media call.wav --whisper-model small
```

## Supported inputs

- **Transcripts:** `.json` (Whisper, faster-whisper or WhisperX, with or without word timings), `.srt`, `.vtt` (`<v Speaker>` tags become speaker labels) and `.txt` (text only, no media alignment).
- **Media:** anything ffmpeg reads. Video outputs `.mp4`. Audio keeps its format for wav, mp3, m4a, flac, ogg and opus.

If SRT/VTT cues don't have timings for each word, word times are estimated inside each cue. Increase **Pad before/after** to be safe, or use *Transcribe locally* for exact word timings.

## Limitations (read before relying on it)

- **Blur modes.** *Speaker only* blurs one face. Pick it in the editor: pause on the speaker, click **Pick speaker face**, then click their face. The export follows that face from frame to frame, including between redactions. A pick only anchors within 2 s of when it was made, so pick again after a camera cut. Without a pick, the largest face is used. *All faces* blurs every detected face, and *Whole frame* is the strictest setting. With *Blur whole frame if no face is found* on (the default), a lost or missed face blurs the whole frame instead of leaving anyone visible.
- Face detection is good but not perfect. Faces that are tiny, heavily turned or hidden may be missed for a few frames. A box is held for 0.75 s after a face was last seen. Always review the output.
- Only the redacted spans are blurred. Visible content such as name badges, slides or on-screen text is not detected.
- Video is re-encoded at constant frame rate (CRF 18). Variable-frame-rate sources are normalised.
- The editor keeps an unredacted transcript loaded in memory, and *Transcribe locally* writes an unredacted transcript to `workspace/`. Delete that folder when you're done if needed.

## Tests

```bash
.venv/bin/python -m unittest discover tests
```

The test creates synthetic video and audio, then checks that the originals are unchanged by hash, the text is redacted, the spans are silent (below −80 dB), the rest is still audible, and metadata is stripped.
