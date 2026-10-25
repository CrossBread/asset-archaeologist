#!/usr/bin/env bash
# Starts Asset Archaeologist and opens it in your browser (Needs Testing). Keep this window open while you use it.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/asset-archaeologist ]; then
  echo "Asset Archaeologist is not set up yet. Double-click setup-macos.command first."
  read -r -p "Press Return to close. " _; exit 1
fi
echo "Starting Asset Archaeologist. Your browser will open in a moment."
echo "Keep this window open while you use it; close it to stop."
.venv/bin/asset-archaeologist serve --open
