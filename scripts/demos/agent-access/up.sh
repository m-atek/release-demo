#!/usr/bin/env bash
# scripts/demo.sh up agent-access: applies infra/demos/9-agent-access (Terraform
# shows its plan and asks first), then builds the coding agent's image.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
"$HERE/tf.sh" apply
"$PY" "$HERE/agent.py" build
echo "Ready: run scripts/demos/agent-access/check.sh"
