#!/usr/bin/env bash
# ============================================================
#  alvaagent launcher — serve index.html on a local server
#  Works in Termux (Android) and on desktop Linux/macOS.
#
#  Usage (Termux / Android — storage mounts ignore +x, so use bash):
#    bash start.sh         # serve on port 8080
#    bash start.sh 9000    # serve on port 9000
#    bash start.sh tui     # chat inside the terminal (no browser)
#
#  Usage (desktop Linux/macOS):
#    ./start.sh            # serve on port 8080
#    ./start.sh tui        # chat inside the terminal
# ============================================================

set -u

# ---------- TUI mode: run the terminal chat client (no browser) ----------
if [ "${1:-}" = "tui" ] || [ "${1:-}" = "--tui" ]; then
  DIR="$(cd "$(dirname "$0")" && pwd)"
  if command -v python3 >/dev/null 2>&1; then
    PY="python3"
  elif command -v python >/dev/null 2>&1; then
    PY="python"
  else
    echo "❌ Python not found (needed for the TUI)."
    exit 1
  fi
  echo "⚡ alvaagent TUI"
  exec "$PY" "$DIR/alvaagent_tui.py"
fi

PORT="${1:-8080}"
case "$PORT" in
  --offline|8080) PORT=8080 ;;
  *[!0-9]*) echo "❌ Invalid port: $1 (use a number, e.g. ./start.sh 9000)"; exit 1 ;;
esac

DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "⚡ alvaagent launcher"
echo "   $DIR/index.html"
echo ""

# ---------- 1. find a python interpreter ----------
PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "❌ Python not found."
  echo "   • Termux:   pkg install python"
  echo "   • Debian:   sudo apt install python3"
  exit 1
fi

# ---------- 2. sanity checks ----------
if [ ! -f "$DIR/index.html" ]; then
  echo "❌ index.html not found in $DIR"
  exit 1
fi

if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "⚠️  Port $PORT is already in use. Try:  ./start.sh 9000"
  exit 1
fi

# ---------- 3. instructions ----------
echo "📡 Serving alvaagent at:"
echo ""
echo "       http://127.0.0.1:$PORT"
echo ""
echo "   • Open that address in Chrome (or any browser) on this device."
echo "   • Same Wi-Fi? Your phone's IP also works, e.g. http://<phone-ip>:$PORT"
echo "   • Press Ctrl+C in this terminal to stop the server."
echo ""

# ---------- 4. best-effort: open a browser automatically ----------
if command -v termux-open-url >/dev/null 2>&1; then
  (termux-open-url "http://127.0.0.1:$PORT" >/dev/null 2>&1 &)
elif command -v xdg-open >/dev/null 2>&1; then
  (xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 &)
elif command -v open >/dev/null 2>&1; then
  (open "http://127.0.0.1:$PORT" >/dev/null 2>&1 &)
fi

# ---------- 5. run the server ----------
cd "$DIR" || exit 1
exec "$PY" -m http.server "$PORT" --bind 0.0.0.0
