#!/usr/bin/env bash
# scripts/demo.sh status leaked-token: see status.py next to this file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/leaked-token/status.py"
