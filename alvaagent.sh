#!/usr/bin/env bash
# ============================================================
#  alvaagent — launch the terminal chat client from anywhere
#
#  Install once (symlink onto Termux PATH so `git pull` keeps it
#  up to date):
#    ln -sf "$PWD/alvaagent" /data/data/com.termux/files/usr/bin/alvaagent
#
#  Then just type:  alvaagent
# ============================================================

set -u

# Resolve this script's real path (handles being symlinked from $PREFIX/bin)
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

# Find a Python interpreter
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "Python not found. Install it:  pkg install python   (Termux)" >&2
  exit 1
fi

if [ ! -f "$DIR/alvaagent_tui.py" ]; then
  echo "alvaagent_tui.py not found next to $DIR" >&2
  exit 1
fi

cd "$DIR" || exit 1
exec "$PY" "$DIR/alvaagent_tui.py" "$@"
