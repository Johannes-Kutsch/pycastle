#!/usr/bin/env bash
# Crontab usage (cron-install.sh writes this for you):
#   0 1 * * * /abs/path/to/repo/pycastle/setup/cron.sh >> <logs_dir>/cron.log 2>&1 # pycastle:/abs/path
# Options:
#   --no-improve   Pass --no-improve to 'pycastle run', suppressing improve-agent dispatch.
set -euo pipefail

cd "$(dirname "$0")/../.."

RUN_EXTRA_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-improve) RUN_EXTRA_ARGS+=(--no-improve) ;;
        *) echo "Usage: cron.sh [--no-improve]" >&2; exit 1 ;;
    esac
done

if [ ! -d ".venv" ]; then
    echo "Error: .venv/ not found. Create it first: python -m venv .venv && .venv/bin/pip install pycastle" >&2
    exit 1
fi

PYCASTLE_HOME="${PYCASTLE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/pycastle}"
mkdir -p "$PYCASTLE_HOME"

(
    flock -w 21600 200

    # Run pip install --upgrade pycastle twice (best-effort): the first call
    # sometimes reports "already up to date" because new releases can take time
    # to propagate across PyPI's CDN; the second call ensures the latest version
    # is installed.  Both calls are best-effort — a failed upgrade does not abort
    # the tick; we prefer running last night's version to skipping the tick entirely.
    .venv/bin/python -m pip install --upgrade pycastle \
        || echo "WARNING: pip upgrade pycastle (attempt 1) failed; continuing with installed version" >&2
    .venv/bin/python -m pip install --upgrade pycastle \
        || echo "WARNING: pip upgrade pycastle (attempt 2) failed; continuing with installed version" >&2

    .venv/bin/pycastle init --refresh
    .venv/bin/pycastle run "${RUN_EXTRA_ARGS[@]+"${RUN_EXTRA_ARGS[@]}"}"
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

if [ -n "$LOG_FILE" ]; then
    if [ -f "$LOG_FILE" ]; then
        trimmed=$(tail -n 10000 "$LOG_FILE" 2>/dev/null) || trimmed=""
        printf '%s\n' "$trimmed" > "$LOG_FILE"
    fi
    LOGS_BASE=$(dirname "$LOG_FILE")
    if [ -d "$LOGS_BASE" ]; then
        find "$LOGS_BASE" -maxdepth 1 -name "*.log" -mtime +"$LOG_RETENTION_DAYS" -delete
    fi
fi
