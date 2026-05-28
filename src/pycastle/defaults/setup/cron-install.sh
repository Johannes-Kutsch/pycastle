#!/usr/bin/env bash
set -euo pipefail

if ! command -v crontab >/dev/null 2>&1; then
    echo "Error: crontab is not on PATH" >&2
    exit 1
fi

REPO_ROOT="$(realpath "$(dirname "$0")/../..")"
cd "$REPO_ROOT"

if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    echo "Error: python not on PATH; activate the project's virtualenv first" >&2
    exit 1
fi

LOG_DIR=$("$PYTHON" -c "
from pathlib import Path
from pycastle.config.loader import load_config, resolve_logs_dir
print(resolve_logs_dir(load_config()))
") || { echo "Error: failed to resolve logs_dir from pycastle config" >&2; exit 1; }

mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/cron.log"
MARKER="# pycastle:$REPO_ROOT"
CRON_LINE="0 1 * * * $REPO_ROOT/pycastle/setup/cron.sh >> $LOG_PATH 2>&1 $MARKER"

kept=""
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        *"$MARKER") ;;
        *) kept+="$line"$'\n' ;;
    esac
done < <(crontab -l 2>/dev/null || true)

printf '%s%s\n' "$kept" "$CRON_LINE" | crontab -
