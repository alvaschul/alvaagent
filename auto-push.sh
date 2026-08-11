#!/usr/bin/env bash
# alvaagent auto-push: commit any changes and push to GitHub.
# Runs from cron. Safe to run frequently — only pushes when there's a diff.
set -euo pipefail

REPO_DIR="/root/alvaagent"
cd "$REPO_DIR"

# Nothing to do if working tree is clean
if git diff --quiet && git diff --cached --quiet; then
    echo "$(date -Is) no changes — nothing to push"
    exit 0
fi

# Stage everything, commit with a timestamped message
git add -A
TS=$(date '+%Y-%m-%d %H:%M')
git commit -q -m "auto: changes from $(hostname) @ $TS" || {
    echo "$(date -Is) commit failed"
    exit 1
}

# Push; surface the result
if git push origin main 2>&1; then
    echo "$(date -Is) pushed successfully"
else
    echo "$(date -Is) PUSH FAILED"
    exit 1
fi
