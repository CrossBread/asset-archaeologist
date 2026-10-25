#!/usr/bin/env bash
# Asset Archaeologist setup for macOS (Needs Testing): double-click this file in Finder.
# If macOS refuses to open it, right-click it and choose Open.
cd "$(dirname "$0")" || exit 1
bash ./setup-unix-common.sh "$@"
echo; read -r -p "Press Return to close this window. " _
