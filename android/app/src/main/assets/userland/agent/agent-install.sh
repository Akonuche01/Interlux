#!/system/bin/sh
# agent-install.sh — verify agent daemon bundled in the userland.
# The APK already extracts agent/ via Userland.kt; no pip needed
# (providers use stdlib urllib; only websockets ships in site-packages).

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$HERE/serve.py" ]; then
  echo "[agent] ERROR: serve.py missing in $HERE" >&2
  exit 1
fi

mkdir -p "$HOME/.interlux/agent"

echo "[agent] done. Run: iagent-launch [port] (default 4600)"
