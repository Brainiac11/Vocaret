#!/usr/bin/env bash
# Vocaret installer (Linux; macOS is best-effort).
#
#   ./install.sh               install or upgrade
#   ./install.sh --cpu         skip the NVIDIA libraries (CPU speech recognition)
#   ./install.sh --no-editors  don't install the VS Code / Cursor extension
#   ./install.sh --no-check    don't run the setup check at the end
#   ./install.sh --uninstall   remove everything this script installed
#
# Installs into ~/.local/share/vocaret (a private Python environment plus a
# copy of the sources), links ~/.local/bin/vocaret, and adds the extension to
# every VS Code-family editor it finds. Nothing needs root.
#
# The editor extension runs this same script (with --no-editors --no-check)
# when someone installs it from a marketplace.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${VOCARET_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/vocaret}"
BIN_DIR="${VOCARET_BIN:-$HOME/.local/bin}"
EDITOR_CLIS=(code code-insiders cursor codium windsurf)
EXTENSION_ID="vocaret.vocaret"
GPU=auto
EDITORS=1
CHECK=1
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --cpu) GPU=no ;;
    --gpu) GPU=yes ;;
    --no-editors) EDITORS=0 ;;
    --no-check) CHECK=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

# The extension id is publisher.name from its manifest, when we have it.
if [ -f "$HERE/extension/package.json" ] && command -v python3 >/dev/null 2>&1; then
  EXTENSION_ID="$(python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); print(p["publisher"]+"."+p["name"])' \
    "$HERE/extension/package.json" 2>/dev/null || echo "$EXTENSION_ID")"
fi

if [ "$UNINSTALL" = 1 ]; then
  [ -x "$BIN_DIR/vocaret" ] && "$BIN_DIR/vocaret" stop >/dev/null 2>&1 || true
  for cli in "${EDITOR_CLIS[@]}"; do
    if command -v "$cli" >/dev/null 2>&1; then
      "$cli" --uninstall-extension "$EXTENSION_ID" >/dev/null 2>&1 \
        && say "removed the extension from $cli" || true
    fi
  done
  rm -f "$BIN_DIR/vocaret"
  rm -rf "$PREFIX"
  say "Vocaret removed. Downloaded speech models stay in ~/.cache/huggingface."
  exit 0
fi

# ------------------------------------------------------------ prerequisites
case "$(uname -s)" in
  Linux)
    command -v pw-record >/dev/null 2>&1 || command -v arecord >/dev/null 2>&1 \
      || command -v rec >/dev/null 2>&1 \
      || warn "no audio recorder found; install pipewire-bin, alsa-utils or sox" ;;
  Darwin)
    command -v rec >/dev/null 2>&1 || command -v ffmpeg >/dev/null 2>&1 \
      || warn "no audio recorder found; run: brew install sox" ;;
  *) warn "this platform is untested" ;;
esac

if [ "$GPU" = auto ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then GPU=yes; else GPU=no; fi
fi
EXTRA=""
[ "$GPU" = yes ] && EXTRA="[gpu]"

# ------------------------------------------------------------------- files
say "Installing into $PREFIX"
mkdir -p "$PREFIX" "$BIN_DIR"
if [ "$HERE" != "$PREFIX/src" ]; then
  rm -rf "$PREFIX/src"
  mkdir -p "$PREFIX/src"
  # A self-contained copy, so the install survives the source being deleted
  # (or the extension being updated). Copy whatever this checkout/bundle has.
  for item in daemon extension scripts tests install.sh README.md LICENSE CHANGELOG.md; do
    [ -e "$HERE/$item" ] && cp -R "$HERE/$item" "$PREFIX/src/"
  done
  find "$PREFIX/src" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  rm -rf "$PREFIX/src/daemon/"*.egg-info "$PREFIX/src/extension/bundled" "$PREFIX/src/extension/node_modules"
fi

# ------------------------------------------------------- python environment
say "Setting up the Python environment (speech recognition${EXTRA:+, with GPU support})"
if command -v uv >/dev/null 2>&1; then
  [ -x "$PREFIX/venv/bin/python" ] || uv venv -q --python ">=3.10" "$PREFIX/venv"
  uv pip install -q --python "$PREFIX/venv/bin/python" --reinstall-package vocaret "$PREFIX/src/daemon$EXTRA"
else
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || { echo "python3 (3.10+) is required" >&2; exit 1; }
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "python3 3.10 or newer is required (found $("$PY" -V 2>&1))" >&2; exit 1; }
  [ -x "$PREFIX/venv/bin/python" ] || "$PY" -m venv "$PREFIX/venv" \
    || { echo "could not create a virtual environment (on Debian/Ubuntu: sudo apt install python3-venv)" >&2; exit 1; }
  "$PREFIX/venv/bin/python" -m pip install -q --upgrade pip
  "$PREFIX/venv/bin/python" -m pip install -q --force-reinstall --no-deps "$PREFIX/src/daemon"
  "$PREFIX/venv/bin/python" -m pip install -q "$PREFIX/src/daemon$EXTRA"
fi
ln -sf "$PREFIX/venv/bin/vocaret" "$BIN_DIR/vocaret"
# An older daemon would keep serving the old code.
"$BIN_DIR/vocaret" stop >/dev/null 2>&1 || true
say "Installed vocaret $("$BIN_DIR/vocaret" --version)"

# ------------------------------------------------------------------ editors
if [ "$EDITORS" = 1 ] && [ -f "$PREFIX/src/scripts/build_vsix.py" ]; then
  VSIX="$("$PREFIX/venv/bin/python" "$PREFIX/src/scripts/build_vsix.py" "$PREFIX/vocaret.vsix")"
  found=0
  for cli in "${EDITOR_CLIS[@]}"; do
    if command -v "$cli" >/dev/null 2>&1; then
      found=1
      if "$cli" --install-extension "$VSIX" --force >/dev/null 2>&1; then
        say "extension installed in $cli"
      else
        warn "could not install the extension in $cli (install $VSIX by hand: Extensions > ... > Install from VSIX)"
      fi
    fi
  done
  [ "$found" = 1 ] || warn "no VS Code-family editor found; the extension is at $VSIX"
fi

# -------------------------------------------------------------------- check
if [ "$CHECK" = 1 ]; then
  say "Checking the setup (the first run downloads the speech model)"
  "$BIN_DIR/vocaret" doctor || warn "see the FAIL line above; 'vocaret doctor' re-runs this check"
  cat <<EOF

Vocaret is installed.
  1. Reload your editor window (Ctrl+Shift+P > "Developer: Reload Window").
  2. Put the caret in a file, press Ctrl+Alt+Space, say what you want typed,
     and press Ctrl+Alt+Space again. Esc cancels.
  3. To use something other than Claude Code, run "Vocaret: Choose Provider and Model".
Uninstall with: $PREFIX/src/install.sh --uninstall
EOF
fi
