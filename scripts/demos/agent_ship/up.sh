#!/usr/bin/env bash
# scripts/demo.sh up agent-ship: see demo.py next to this file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
exec "$PY" "$ROOT/scripts/demos/agent_ship/demo.py" up "$@"
