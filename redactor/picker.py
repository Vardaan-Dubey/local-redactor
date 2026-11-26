"""Native "Open file" / "Choose folder" dialogs for the local editor.

The browser can't hand a page the real path of a file the user picks, and the
redactor works on paths (originals are read in place, never copied). So the
localhost server opens the operating system's own dialog and returns the path.

Every dialog runs in a child process - macOS and Tk only allow GUI calls on a
process's main thread, and the server answers requests on worker threads.
Nothing here touches the network.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path

MEDIA_EXT = ["mp4", "mov", "m4v", "mkv", "webm", "avi", "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus"]
TRANSCRIPT_EXT = ["srt", "vtt", "json", "txt"]
KINDS = {
    "media": ("Choose a video or audio file", MEDIA_EXT),
    "transcript": ("Choose a transcript", TRANSCRIPT_EXT),
    "folder": ("Choose an output folder", None),
}

_lock = threading.Lock()  # one dialog at a time


class PickerUnavailable(RuntimeError):
    pass


def pick(kind: str, start: str | None = None) -> str | None:
    """Show a native dialog. Returns the chosen path, or None if cancelled."""
    if kind not in KINDS:
        raise ValueError(f"Unknown picker kind: {kind}")
    title, exts = KINDS[kind]
    folder = kind == "folder"
    start_dir = _start_dir(start)
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A file dialog is already open")
    try:
        if sys.platform == "darwin":
            out = _mac(title, exts, folder, start_dir)
        elif sys.platform == "win32":
            out = _windows(title, exts, folder, start_dir)
        else:
            out = _linux(title, exts, folder, start_dir)
    finally:
        _lock.release()
    out = (out or "").strip()
    return out or None


def _start_dir(start: str | None) -> str | None:
    if not start:
        return None
    p = Path(start).expanduser()
    p = p if p.is_dir() else p.parent
    return str(p) if p.is_dir() else None


def _run(cmd: list[str], cancel_codes=(1,)) -> str | None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout
    if r.returncode in cancel_codes:
        return None
    raise RuntimeError((r.stderr or "File dialog failed").strip())


# ---------------------------------------------------------------- macOS ----
def _as_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mac(title, exts, folder, start_dir):
    what = "folder" if folder else "file"
    script = f"choose {what} with prompt {_as_str(title)}"
    if exts:
        script += " of type {" + ", ".join(_as_str(e) for e in exts) + "}"
    if start_dir:
        script += f" default location (POSIX file {_as_str(start_dir)})"
    # "activate" brings osascript's dialog in front of the browser window.
    r = subprocess.run(["osascript", "-e", "activate", "-e", f"POSIX path of ({script})"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout
    if "-128" in r.stderr:  # user pressed Cancel
        return None
    raise RuntimeError((r.stderr or "File dialog failed").strip())


# -------------------------------------------------------------- Windows ----
def _ps_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _windows(title, exts, folder, start_dir):
    # A hidden top-most form owns the dialog so it opens in front of the browser.
    lines = [
        "Add-Type -AssemblyName System.Windows.Forms",
        "[System.Windows.Forms.Application]::EnableVisualStyles()",
        "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; ShowInTaskbar=$false}",
    ]
    if folder:
        lines += [
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
            f"$d.Description = {_ps_str(title)}",
            "$d.ShowNewFolderButton = $true",
        ]
        if start_dir:
            lines.append(f"$d.SelectedPath = {_ps_str(start_dir)}")
        result = "$d.SelectedPath"
    else:
        pattern = ";".join(f"*.{e}" for e in exts)
        lines += [
            "$d = New-Object System.Windows.Forms.OpenFileDialog",
            f"$d.Title = {_ps_str(title)}",
            f"$d.Filter = {_ps_str(f'Supported files ({pattern})|{pattern}|All files (*.*)|*.*')}",
        ]
        if start_dir:
            lines.append(f"$d.InitialDirectory = {_ps_str(start_dir)}")
        result = "$d.FileName"
    lines += [
        "if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) "
        f"{{ [Console]::OutputEncoding = [Text.Encoding]::UTF8; Write-Output {result} }}",
        "$owner.Dispose()",
    ]
    return _run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                 "-Command", "; ".join(lines)], cancel_codes=())


# ---------------------------------------------------------------- Linux ----
def _linux(title, exts, folder, start_dir):
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={title}"]
        if folder:
            cmd.append("--directory")
        elif exts:
            cmd += [f"--file-filter=Supported | {' '.join('*.' + e for e in exts)}",
                    "--file-filter=All files | *"]
        if start_dir:
            cmd.append(f"--filename={start_dir.rstrip('/')}/")
        return _run(cmd)
    if shutil.which("kdialog"):
        where = start_dir or str(Path.home())
        if folder:
            return _run(["kdialog", "--title", title, "--getexistingdirectory", where])
        return _run(["kdialog", "--title", title, "--getopenfilename", where,
                     " ".join("*." + e for e in exts)])
    return _tk(title, exts, folder, start_dir)


_TK = """
import sys, tkinter as tk
from tkinter import filedialog
title, folder, start, exts = sys.argv[1], sys.argv[2] == "1", sys.argv[3] or None, sys.argv[4]
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
if folder:
    p = filedialog.askdirectory(title=title, initialdir=start, mustexist=True)
else:
    pats = " ".join("*." + e for e in exts.split(",") if e)
    p = filedialog.askopenfilename(title=title, initialdir=start,
                                   filetypes=[("Supported files", pats), ("All files", "*")])
print(p or "")
"""


def _tk(title, exts, folder, start_dir):
    r = subprocess.run([sys.executable, "-c", _TK, title, "1" if folder else "0",
                        start_dir or "", ",".join(exts or [])], capture_output=True, text=True)
    if r.returncode != 0:
        raise PickerUnavailable("No file dialog available (install zenity, or type the path instead)")
    return r.stdout
