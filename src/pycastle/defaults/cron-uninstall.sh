#!/usr/bin/env bash
set -euo pipefail

if ! command -v crontab >/dev/null 2>&1; then
    echo "Error: crontab is not on PATH" >&2
    exit 1
fi

REPO_ROOT="$(realpath "$(dirname "$0")/..")"
MARKER="# pycastle:$REPO_ROOT"

kept=""
matched=0
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        *"$MARKER") matched=1 ;;
        *) kept+="$line"$'\n' ;;
    esac
done < <(crontab -l 2>/dev/null || true)

if [ "$matched" -eq 0 ]; then
    exit 0
fi

if [ -n "$kept" ]; then
    printf '%s' "$kept" | crontab -
else
    crontab -r 2>/dev/null || true
fi
