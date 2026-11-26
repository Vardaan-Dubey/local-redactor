#!/usr/bin/env bash
# Kept for compatibility: ./start.sh sets up automatically on first run.
# This runs setup without starting the editor.
cd "$(dirname "$0")"
exec ./start.sh setup --whisper-model "${WHISPER_MODEL:-small}"
