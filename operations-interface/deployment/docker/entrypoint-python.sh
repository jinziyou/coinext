#!/bin/sh
# Shared Python service entrypoint: load lifecycle PYTHONPATH, optional service suffix, exec CMD.
# Source of truth for the base path: /etc/coinext/pythonpath.env (from pythonpath.env in this dir).
set -eu
if [ -f /etc/coinext/pythonpath.env ]; then
  # shellcheck disable=SC1091
  set -a
  . /etc/coinext/pythonpath.env
  set +a
fi
if [ -n "${COINEXT_SERVICE_PYTHONPATH:-}" ]; then
  PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}${COINEXT_SERVICE_PYTHONPATH}"
  export PYTHONPATH
fi
exec "$@"
