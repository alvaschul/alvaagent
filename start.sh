#!/usr/bin/env bash
# ============================================================
#  alvaagent launcher — terminal chat client (TUI mode)
#  Works in Termux (Android) and on desktop Linux/macOS.
#
#  Usage (Termux / Android — storage mounts ignore +x, so use bash):
#    bash start.sh         # launch the TUI
#    bash start.sh tui     # same as above
# ============================================================

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------- find a python interpreter ----------
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "Python not found. Install it:  pkg install python   (Termux)"
  exit 1
fi

if [ ! -f "$DIR/alvaagent_tui.py" ]; then
  echo "alvaagent_tui.py not found in $DIR"
  exit 1
fi

echo "alvaagent TUI"
exec "$PY" "$DIR/alvaagent_tui.py"
