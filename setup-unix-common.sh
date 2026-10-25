#!/usr/bin/env bash
# Shared by setup-macos.command and setup-linux.sh. Not meant to be run on its own.
# Options: --yes (answer yes to everything), --cpu (CPU torch even with an NVIDIA GPU)
set -u
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP"
YES=0; CPU=0
for a in "$@"; do case "$a" in --yes|-y) YES=1;; --cpu) CPU=1;; esac; done

say()  { printf '\n\033[36m== %s\033[0m\n' "$1"; }
ask()  { [ "$YES" = 1 ] && return 0; read -r -p "$1 [Y/n] " r; [ -z "$r" ] || [[ "$r" =~ ^[Yy] ]]; }
fail() { printf '\n\033[31mSetup stopped: %s\033[0m\n' "$1"; exit 1; }

echo "Asset Archaeologist setup"
echo "Installs a private copy of Python and the packages it needs into $APP/.venv"
echo "(nothing system-wide), then checks for Blender."

say "Step 1 of 5: uv (installs Python and packages)"
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
[ -z "$UV" ] && [ -x "$HOME/.cargo/bin/uv" ] && UV="$HOME/.cargo/bin/uv"
if [ -z "$UV" ]; then
  echo "uv is not installed. It is a single program from Astral (https://docs.astral.sh/uv/)."
  ask "Download and install uv now?" || fail "uv is needed. Install it from https://docs.astral.sh/uv/ and run setup again."
  if command -v curl >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh
  else wget -qO- https://astral.sh/uv/install.sh | sh; fi
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || fail "uv did not install. Install it from https://docs.astral.sh/uv/ and run setup again."
fi
echo "uv: $UV"

say "Step 2 of 5: Python 3.12 environment"
PY="$APP/.venv/bin/python"
if [ ! -x "$PY" ]; then
  "$UV" venv --python 3.12 "$APP/.venv" || fail "could not create the Python environment."
else echo "already there: $PY"; fi

say "Step 3 of 5: packages (the long step)"
if [ "$(uname -s)" = "Darwin" ]; then
  echo "macOS: the standard torch build uses the Apple GPU when there is one."
  "$UV" pip install --python "$PY" torch torchvision || fail "torch did not install (see the messages above)."
else
  INDEX="https://download.pytorch.org/whl/cpu"
  if [ "$CPU" = 0 ] && command -v nvidia-smi >/dev/null; then
    V="$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9]*\)\.\([0-9]*\).*/\1 \2/p' | head -1)"
    if [ -n "$V" ]; then
      set -- $V; C=$(( $1 * 100 + $2 ))
      if [ $C -ge 1208 ]; then INDEX="https://download.pytorch.org/whl/cu128"
      elif [ $C -ge 1206 ]; then INDEX="https://download.pytorch.org/whl/cu126"
      elif [ $C -ge 1108 ]; then INDEX="https://download.pytorch.org/whl/cu118"; fi
      echo "NVIDIA GPU found (driver supports CUDA $1.$2)."
    fi
  fi
  case "$INDEX" in *cpu) echo "No usable NVIDIA GPU: installing the CPU build (works, just slower).";;
                   *) echo "Installing the GPU build of torch from $INDEX";; esac
  "$UV" pip install --python "$PY" torch torchvision --index-url "$INDEX" || fail "torch did not install (see the messages above)."
fi
"$UV" pip install --python "$PY" -e "$APP" || fail "the app did not install (see the messages above)."
AA="$APP/.venv/bin/asset-archaeologist"

say "Step 4 of 5: Blender (draws the thumbnails)"
BL="$("$AA" config --json | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("blender_found") or "")')"
PORT="$("$AA" config --json | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("port"))')"
if [ -n "$BL" ]; then
  echo "Found: $BL"
else
  echo "Blender 4.2 or newer is needed and was not found."
  if [ "$(uname -s)" = "Darwin" ]; then
    if command -v brew >/dev/null && ask "Install Blender now with Homebrew (brew install --cask blender)?"; then
      brew install --cask blender
    else
      echo "Download it from https://www.blender.org/download/ , drag it into Applications, then run setup again."
      [ "$YES" = 1 ] || open "https://www.blender.org/download/" 2>/dev/null || true
    fi
  else
    if command -v snap >/dev/null && ask "Install Blender now with snap (sudo snap install blender --classic)?"; then
      sudo snap install blender --classic
    else
      echo "Download it from https://www.blender.org/download/ (distribution packages are often older than 4.2),"
      echo "unpack it, and set its path on the Settings screen of the page, or run setup again."
    fi
  fi
fi

say "Step 5 of 5: search model (SigLIP, about 800 MB, downloaded once) and a final check"
"$AA" doctor --fetch-model && OK=1 || OK=0

echo
if [ "$OK" = 1 ]; then echo "Setup finished."; else echo "Setup finished, but the check above found something to fix (lines marked FAIL)."; fi
echo "Next: run the start launcher in this folder. Your browser opens http://127.0.0.1:$PORT/ ;"
echo "choose your library folders there and press Start scan."
echo "For AI agents: $AA find \"wooden barrel\" --json   or   http://127.0.0.1:$PORT/llms.txt"
