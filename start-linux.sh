#!/usr/bin/env bash
# Starts Asset Archaeologist and opens it in your browser (Needs Testing). Keep the terminal open while you use it.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/asset-archaeologist ]; then
  echo "Asset Archaeologist is not set up yet. Run ./setup-linux.sh first."; exit 1
fi
echo "Starting Asset Archaeologist. Your browser will open in a moment. Press Ctrl+C to stop."
exec .venv/bin/asset-archaeologist serve --open
