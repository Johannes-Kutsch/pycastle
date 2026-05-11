#!/usr/bin/env bash
# Crontab usage:
#   0 * * * * /absolute/path/to/repo/pycastle/setup/cron.sh >> /path/to/logfile.log 2>&1
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

    # Install consuming project dependencies (ADR 0001 fallback chain)
    .venv/bin/python -m pip install -e ".[dev]" || .venv/bin/python -m pip install -r requirements.txt

    .venv/bin/pycastle init --refresh
    .venv/bin/pycastle build
    .venv/bin/pycastle run
) 200>"$PYCASTLE_HOME/.cron.lock"
