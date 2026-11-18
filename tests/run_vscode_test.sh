#!/usr/bin/env bash
# Runs tests/vscode/index.js inside a real VS Code with a throwaway profile.
# Usage: tests/run_vscode_test.sh <speech.wav> [path-to-code-cli]
# Uses Claude Code (your login) for the model steps, plus a local stand-in
# server for the API-key provider step.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WAV="$(realpath "$1")"
CODE="${2:-code}"
T="$(mktemp -d /tmp/vocaret-vsc.XXXXXX)"
export VOCARET_SOCKET="$T/vocaret.sock"
export VOCARET_TEST_REPORT="$T/report.json"
# Run this checkout's daemon code with an environment that has its dependencies.
PY="${VOCARET_TEST_PYTHON:-$HOME/.local/share/vocaret/venv/bin/python}"
printf '#!/usr/bin/env bash\nPYTHONPATH="%s/daemon" exec "%s" -m vocaret "$@"\n' "$HERE" "$PY" > "$T/vocaret"
chmod +x "$T/vocaret"
export VOCARET_TEST_LAUNCHER="$T/vocaret"
export VOCARET_TEST_WAV="$WAV"
export XDG_STATE_HOME="$T/state"

# 1. The packaged VSIX must install cleanly.
python3 "$HERE/scripts/build_vsix.py" "$T/vocaret.vsix" >/dev/null
"$CODE" --extensions-dir "$T/ext" --user-data-dir "$T/data" --install-extension "$T/vocaret.vsix" 2>&1 | tail -1
"$CODE" --extensions-dir "$T/ext" --user-data-dir "$T/data" --list-extensions --show-versions 2>/dev/null | grep -i vocaret

# 2. Drive the extension inside the editor.
mkdir -p "$T/ws"
"$CODE" --extensions-dir "$T/ext-dev" --user-data-dir "$T/data" \
  --disable-workspace-trust --skip-welcome --skip-release-notes --disable-telemetry \
  --new-window --wait \
  --extensionDevelopmentPath="$HERE/extension" \
  --extensionTestsPath="$HERE/tests/vscode/index.js" "$T/ws" >"$T/code.log" 2>&1 &
CODE_PID=$!
for _ in $(seq 1 300); do
  [ -f "$VOCARET_TEST_REPORT" ] && break
  kill -0 "$CODE_PID" 2>/dev/null || break
  sleep 1
done
sleep 2
"$T/vocaret" stop >/dev/null 2>&1 || true
if [ -f "$VOCARET_TEST_REPORT" ]; then
  cat "$VOCARET_TEST_REPORT"
else
  echo "NO REPORT. editor log:"; tail -30 "$T/code.log"
  echo "daemon log:"; tail -30 "$T/state/vocaret/daemon.log" 2>/dev/null || true
  exit 1
fi
echo "artifacts: $T"
