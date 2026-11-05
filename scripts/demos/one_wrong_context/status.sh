#!/usr/bin/env bash
# scripts/demo.sh status one-wrong-context: see demo.py next to this file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/one_wrong_context/demo.py" status "$@"
