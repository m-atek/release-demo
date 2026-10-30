#!/usr/bin/env bash
# Video 9: a fresh workspace and AWS session, then Claude Code in the coding
# agent's container. Prints one line, the role and when its session ends.
# See agent.py next to this file. Sign in first: aws sso login --profile release-demo
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
exec "$PY" "$ROOT/scripts/demos/agent-access/agent.py" run
