#!/usr/bin/env bash
# alvaagent fix: launch the CORRECT (git-cloned) version and carry over your config.
# Run from Termux with:  bash ~/alva_fix.sh
set -u

REPO="$HOME/alvaagent"
OLD="$HOME/.alvaagent"
NEW="$REPO/.alvaagent"

# 1) stop any running old/duplicate instance
pkill -f alvaagent_tui 2>/dev/null || true
sleep 1

# 2) carry over API key + settings from the stale hidden install (if present)
mkdir -p "$NEW"
if [ -f "$OLD/.alvaagent/config.json" ] && [ ! -f "$NEW/config.json" ]; then
  cp "$OLD/.alvaagent/config.json" "$NEW/config.json"
  echo "[ok] carried over config (api key preserved) from old install"
elif [ -f "$NEW/config.json" ]; then
  echo "[ok] config already present at $NEW/config.json"
else
  echo "[!] no config found — you'll be prompted to set up a provider on first run"
fi

# 3) ensure 'rich' is available (Hermes-style panels depend on it)
python3 -c "import rich" 2>/dev/null || pip install --user rich 2>/dev/null || pip install rich 2>/dev/null || true
if ! python3 -c "import rich" 2>/dev/null; then
  echo "[!] 'rich' not importable - panels may fall back; try: pip install rich"
fi

# 4) launch the correct version from the git repo
cd "$REPO" || { echo "cd $REPO failed"; exit 1; }
echo "[ok] launching from: $(pwd)"
exec python3 alvaagent_tui.py
