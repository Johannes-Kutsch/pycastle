#!/usr/bin/env bash
set -euo pipefail

_REMOTE_NAME="${AUTOFIX_REMOTE:-origin}"
_REF="${GITHUB_REF:-${AUTOFIX_REF:-}}"
_WORKING_TREE="${AUTOFIX_WORKING_TREE:-$(pwd)}"
_SCRIPT_NAME="ci-autofix"

cd "$_WORKING_TREE"

if [ -z "$_REF" ]; then
    echo "error: unable to determine ref context; set GITHUB_REF or AUTOFIX_REF" >&2
    exit 1
fi

if ! command -v ruff >/dev/null 2>&1; then
    echo "error: ruff is required in PATH" >&2
    exit 1
fi

if ! git remote get-url "$_REMOTE_NAME" >/dev/null 2>&1; then
    echo "error: remote '$_REMOTE_NAME' not configured" >&2
    exit 1
fi

ruff format
ruff check --fix

if git diff --quiet && git diff --cached --quiet; then
    echo "proceed"
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        echo "status=proceed" >> "$GITHUB_OUTPUT"
    fi
    exit 0
fi

if ! git config user.name >/dev/null; then
    git config user.name "$_SCRIPT_NAME"
fi
if ! git config user.email >/dev/null; then
    git config user.email "$_SCRIPT_NAME@users.noreply.github.com"
fi

git add -A

git commit -m "ci: apply ruff auto-fixes"

if [[ "$_REF" == refs/heads/main ]]; then
    git push "$_REMOTE_NAME" HEAD:refs/heads/main
elif [[ "$_REF" == refs/tags/v* ]]; then
    _TAG_NAME="${_REF#refs/tags/}"
    git tag -f "$_TAG_NAME" HEAD
    git push "$_REMOTE_NAME" HEAD:refs/heads/main
    git push "$_REMOTE_NAME" "$_TAG_NAME" --force
else
    echo "error: unsupported ref context '$_REF'" >&2
    exit 1
fi

echo "fix-pushed"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "status=fix-pushed" >> "$GITHUB_OUTPUT"
fi
