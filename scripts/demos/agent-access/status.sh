#!/usr/bin/env bash
# scripts/demo.sh status agent-access: the role, the image's Claude Code
# version and orders-service-production's live alias.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/agent-access/agent_access.py" status
