#!/usr/bin/env bash
# Crontab usage (cron-install.sh writes this for you):
#   0 1 * * * /abs/path/to/repo/pycastle/setup/cron.sh >> <logs_dir>/cron.log 2>&1 # pycastle:/abs/path
set -euo pipefail

cd "$(dirname "$0")/../.."

if [ ! -d ".venv" ]; then
    echo "Error: .venv/ not found. Create it first: python -m venv .venv && .venv/bin/pip install pycastle" >&2
    exit 1
fi

# Run pip install --upgrade pycastle twice (best-effort): the first call
# sometimes reports "already up to date" because new releases can take time
# to propagate across PyPI's CDN; the second call ensures the latest version
# is installed.  Both calls are best-effort — a failed upgrade does not abort
# the tick; we prefer running last night's version to skipping the tick entirely.
.venv/bin/python -m pip install --upgrade pycastle \
    || echo "WARNING: pip upgrade pycastle (attempt 1) failed; continuing with installed version" >&2
.venv/bin/python -m pip install --upgrade pycastle \
    || echo "WARNING: pip upgrade pycastle (attempt 2) failed; continuing with installed version" >&2

exec .venv/bin/pycastle cron "$@"
