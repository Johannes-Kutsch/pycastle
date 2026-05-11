#!/usr/bin/env bash
set -euo pipefail

if ! command -v crontab >/dev/null 2>&1; then
    echo "Error: crontab is not on PATH" >&2
    exit 1
fi

REPO_ROOT="$(realpath "$(dirname "$0")/../..")"
MARKER="# pycastle:$REPO_ROOT"
CRON_LINE="0 1 * * * $REPO_ROOT/pycastle/setup/cron.sh $MARKER"

kept=""
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        *"$MARKER") ;;
        *) kept+="$line"$'\n' ;;
    esac
done < <(crontab -l 2>/dev/null || true)

printf '%s%s\n' "$kept" "$CRON_LINE" | crontab -
