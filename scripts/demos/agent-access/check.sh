#!/usr/bin/env bash
# Video 9's acceptance checks 1 to 5 in a fresh container, one ok or FAIL line
# each, with no model call. --expired runs check 8, an hour later, in the
# container this run leaves going. See agent_access.py next to this file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
exec "$PY" "$ROOT/scripts/demos/agent-access/agent_access.py" check "$@"
