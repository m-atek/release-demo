#!/usr/bin/env bash
# scripts/demo.sh up can-i: see demo.py next to this file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/can_i/demo.py" up "$@"
