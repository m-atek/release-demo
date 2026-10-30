#!/usr/bin/env bash
# scripts/demo.sh open-pr github-actions: the take's pull request, as you. See pr.py next to this file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/github_actions/pr.py" open "$@"
