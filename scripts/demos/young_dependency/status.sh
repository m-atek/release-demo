#!/usr/bin/env bash
# scripts/demo.sh status young-dependency: which real package is under the rule now, read from PyPI. See pick.py.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/young_dependency/pick.py" show
