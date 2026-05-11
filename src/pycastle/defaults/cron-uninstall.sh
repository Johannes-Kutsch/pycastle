#!/usr/bin/env bash
set -euo pipefail

if ! command -v crontab >/dev/null 2>&1; then
    echo "Error: crontab is not on PATH" >&2
    exit 1
fi

REPO_ROOT="$(realpath "$(dirname "$0")/..")"
MARKER="# pycastle:$REPO_ROOT"

current="$(crontab -l 2>/dev/null || true)"

if ! printf '%s\n' "$current" | grep -qF "$MARKER"; then
    exit 0
fi

filtered="$(printf '%s\n' "$current" | grep -Fv "$MARKER" || true)"

if [ -n "$filtered" ]; then
    printf '%s\n' "$filtered" | crontab -
else
    crontab -r 2>/dev/null || true
fi
