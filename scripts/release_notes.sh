#!/usr/bin/env bash
# Print GitHub release notes for a tag: its CHANGELOG.md section plus
# install steps. Usage: scripts/release_notes.sh v0.2.0
set -euo pipefail
TAG="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECTION="$(awk -v h="## ${TAG#v}" '$0 == h {on = 1; next} /^## / {on = 0} on' "$ROOT/CHANGELOG.md")"
[ -n "$(printf '%s' "$SECTION" | tr -d '[:space:]')" ] || { echo "no CHANGELOG.md section for ${TAG#v}" >&2; exit 1; }
printf '%s\n' "$SECTION" | sed -e '/./,$!d'
cat <<EOF

## Install

Requires Linux (macOS is best-effort), Python 3.10+ and a microphone.

- **Extension only:** download \`vocaret-$TAG.vsix\` below. In VS Code or
  Cursor, open Extensions, then ... > Install from VSIX. The first time you
  press Ctrl+Alt+Space it offers to set up its speech engine.
- **From source:** \`git clone https://github.com/Brainiac11/Vocaret.git && cd Vocaret && ./install.sh\`

Then run **Vocaret: Choose Provider and Model** to pick Claude Code, Codex,
an API provider or a local model. See the
[README](https://github.com/Brainiac11/Vocaret#readme) for details.
EOF
