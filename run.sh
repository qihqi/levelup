#!/bin/sh
# Use the shared home environment if present; PYTHON_BIN can override it.
set -eu
cd "$(dirname "$0")"
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$HOME/.venv/bin/python" ]; then
    PYTHON_BIN="$HOME/.venv/bin/python"
  else
    PYTHON_BIN=python3
  fi
fi
exec "$PYTHON_BIN" -m levelup "$@"
