#!/usr/bin/env bash
# scripts/demo.sh down one-wrong-context [--yes]: see demo.py next to this file. It says
# what it deletes, and asks. k3s and catalog stay: scripts/demo.sh down kubernetes stops them.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/one_wrong_context/demo.py" down "$@"
