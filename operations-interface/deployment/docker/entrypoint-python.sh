#!/bin/sh
# Shared Python service entrypoint.
# Workspace packages are installed into the image venv via `uv sync` (no bulk PYTHONPATH).
# Only the thin service app directory is appended when COINEXT_SERVICE_PYTHONPATH is set.
set -eu
if [ -n "${COINEXT_SERVICE_PYTHONPATH:-}" ]; then
  PYTHONPATH="${COINEXT_SERVICE_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
fi
exec "$@"
