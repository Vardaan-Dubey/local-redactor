#!/usr/bin/env bash
# Local Redactor - start script for macOS and Linux.
# First run: installs everything into this folder (+ uv into ~/.local/bin).
# No admin rights, Homebrew or sudo. Later runs start offline.
# Any arguments are passed to the tool, e.g.
#   ./start.sh --media ~/Desktop/interview.mp4 --transcript ~/Desktop/interview.srt
#   ./start.sh redact --media call.wav --transcript call.json --find "Jane Doe"
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
PY=.venv/bin/python

if [ ! -x "$PY" ] || ! cmp -s requirements.txt .venv/.installed-requirements; then
  echo "==> First-time setup (needs internet once; a few minutes)"
  if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv (Python installer) into ~/.local/bin"
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    else
      echo "Need curl or wget to download uv. Install one, or get uv from https://docs.astral.sh/uv/"; exit 1
    fi
  fi
  echo "==> Creating private Python 3.12 environment in .venv"
  uv venv --python 3.12 --allow-existing .venv
  uv pip install --python "$PY" -r requirements.txt
  cp requirements.txt .venv/.installed-requirements
fi

# Fetches the face and Whisper models only if they are missing.
"$PY" -m redactor setup --whisper-model "${WHISPER_MODEL:-small}"

export HF_HUB_OFFLINE=1
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m redactor "$@"
