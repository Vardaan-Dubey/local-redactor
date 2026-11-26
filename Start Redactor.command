#!/bin/bash
# macOS: double-click to set up (first time) and open the editor.
cd "$(dirname "$0")"
bash ./start.sh "$@" || { echo; read -r -p "Something went wrong (see above). Press Enter to close."; }
