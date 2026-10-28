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
    if [ -e "$DEST" ] || [ -L "$DEST" ]; then
      rm -f "$DEST"
      echo "Removed $DEST"
    else
      echo "Not installed ($DEST doesn't exist)"
    fi
    echo "Quit iTerm2 (or stop the script under Scripts → Manage) to stop the running copy."
    ;;
  "")
    mkdir -p "$DEST_DIR"
    rm -f "$DEST"  # Replaces a symlink instead of copying onto its target.
    cp "$SRC" "$DEST"
    echo "Installed $DEST"
    echo "Quit and reopen iTerm2 to load it (this also replaces a running older copy)."
    ;;
  *)
    echo "usage: $0 [--uninstall]" >&2
    exit 2
    ;;
esac
