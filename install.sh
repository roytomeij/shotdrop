#!/bin/sh
# Install (or uninstall) ShotDrop as an iTerm2 AutoLaunch script.
#
#   ./install.sh              copy shotdrop.py into the AutoLaunch folder
#   ./install.sh --uninstall  remove it again
set -eu

DEST_DIR="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch"
DEST="$DEST_DIR/shotdrop.py"
SRC="$(cd "$(dirname "$0")" && pwd)/shotdrop.py"

case "${1:-}" in
  --uninstall)
    rm -f "$DEST"
    echo "Removed $DEST"
    echo "Quit iTerm2 (or stop the script under Scripts → Manage) to stop the running copy."
    ;;
  "")
    mkdir -p "$DEST_DIR"
    cp "$SRC" "$DEST"
    echo "Installed $DEST"
    echo "Restart iTerm2, or start it now from Scripts → AutoLaunch → shotdrop.py."
    ;;
  *)
    echo "usage: $0 [--uninstall]" >&2
    exit 2
    ;;
esac
