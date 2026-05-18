#!/usr/bin/env bash
# Crontab usage (cron-install.sh writes this for you):
#   0 1 * * * /abs/path/to/repo/pycastle/setup/cron.sh >> <logs_dir>/cron.log 2>&1 # pycastle:/abs/path
set -euo pipefail

cd "$(dirname "$0")/../.."

if [ ! -d ".venv" ]; then
    echo "Error: .venv/ not found. Create it first: python -m venv .venv && .venv/bin/pip install pycastle" >&2
    exit 1
fi

PYCASTLE_HOME="${PYCASTLE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/pycastle}"
mkdir -p "$PYCASTLE_HOME"

(
    flock -w 21600 200

    # Run pip install --upgrade pycastle twice: the first call sometimes reports
    # "already up to date" because new releases can take time to propagate across
    # PyPI's CDN; the second call ensures the latest version is installed.
    .venv/bin/python -m pip install --upgrade pycastle
    .venv/bin/python -m pip install --upgrade pycastle

    .venv/bin/pycastle init --refresh
    .venv/bin/pycastle build
    .venv/bin/pycastle run
) 200>"$PYCASTLE_HOME/.cron.lock"

# Trim cron.log to the last 10000 lines and sweep *.log files older than 30 days.
# Resolved via the same logs_dir the cron line redirects into.
LOG_RETENTION_DAYS=30
LOG_FILE=$(.venv/bin/python -c "
from pathlib import Path
from pycastle.config.loader import load_config
p = load_config().logs_dir
base = p if p.is_absolute() else (Path.cwd() / p).resolve()
print(base / 'cron.log')
" 2>/dev/null) || LOG_FILE=""

if [ -n "$LOG_FILE" ] && [ -f "$LOG_FILE" ]; then
    trimmed=$(tail -n 10000 "$LOG_FILE" 2>/dev/null) || trimmed=""
    printf '%s\n' "$trimmed" > "$LOG_FILE"
fi

LOGS_BASE=$(dirname "$LOG_FILE")
if [ -n "$LOG_FILE" ] && [ -d "$LOGS_BASE" ]; then
    find "$LOGS_BASE" -maxdepth 1 -name "*.log" -mtime +"$LOG_RETENTION_DAYS" -delete
fi
