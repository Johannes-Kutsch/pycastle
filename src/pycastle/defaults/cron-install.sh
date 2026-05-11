#!/usr/bin/env bash
set -euo pipefail

if ! command -v crontab >/dev/null 2>&1; then
    echo "Error: crontab is not on PATH" >&2
    exit 1
fi

REPO_ROOT="$(realpath "$(dirname "$0")/..")"
MARKER="# pycastle:$REPO_ROOT"
CRON_LINE="0 1 * * * $REPO_ROOT/pycastle/cron.sh $MARKER"

current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | grep -Fv "$MARKER" || true)"

if [ -n "$filtered" ]; then
    printf '%s\n%s\n' "$filtered" "$CRON_LINE" | crontab -
else
    printf '%s\n' "$CRON_LINE" | crontab -
fi
