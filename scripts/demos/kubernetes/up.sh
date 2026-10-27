#!/usr/bin/env bash
# scripts/demo.sh up kubernetes: start k3s on the tools host and set up the
# cluster, then wait until it answers and print "Ready". The tools host must
# be up first: scripts/tools.sh up.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" --timeout 600 python3 scripts/demos/kubernetes/host/k3s.py up
