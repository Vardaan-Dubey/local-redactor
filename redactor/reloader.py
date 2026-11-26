"""Auto-restart for the editor server.

`python -m redactor` (what the start scripts run) starts the server in a child
process and watches the redactor/*.py files. When one changes, the server is
restarted on the same port, so a browser refresh picks up the new code - no
need to stop and re-run the start script. The page files under static/ are
read from disk on every request and never need a restart.

Standard library only; polls file times once a second (no extra packages).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent
CHILD_ENV = "REDACTOR_SERVER_CHILD"


def is_child() -> bool:
    return os.environ.get(CHILD_ENV) == "1"


def _code_times() -> dict[Path, int]:
    times = {}
    for p in PKG.rglob("*.py"):
        try:
            times[p] = p.stat().st_mtime_ns
        except OSError:  # deleted mid-scan
            pass
    return times


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def supervise(argv: list[str]) -> int:
    """Run `python -m redactor <argv>` and restart it whenever the code changes."""
    env = dict(os.environ, **{CHILD_ENV: "1"})
    restarted = False
    while True:
        # Only the first start opens a browser tab; after that, refreshing is enough.
        args = argv + (["--no-browser"] if restarted and "--no-browser" not in argv else [])
        child = subprocess.Popen([sys.executable, "-m", "redactor", *args], env=env)
        seen = _code_times()
        try:
            while True:
                time.sleep(1)
                if _code_times() != seen:
                    print("\n==> Code changed - restarting the server. Refresh the page.", flush=True)
                    _stop(child)
                    break
                code = child.poll()
                if code is None:
                    continue
                if not restarted:
                    return code  # stopped or failed on first start: let the start script report it
                # Crashed after a code change (e.g. a typo): wait for the next fix.
                print("==> Server stopped (see above). Waiting for a code change to restart.", flush=True)
                while _code_times() == seen:
                    time.sleep(1)
                break
        except KeyboardInterrupt:
            _stop(child)
            return 0
        restarted = True
