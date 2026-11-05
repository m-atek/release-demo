#!/usr/bin/env bash
# scripts/demo.sh up hotfix-room: the namespace forecast and the pipeline's rights
# in it, then "Ready". The cluster must be up first: scripts/demo.sh up kubernetes.
# scripts/reset.py hotfix-room then puts the two services where a take starts.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" --timeout 300 \
  --folder scripts/demos/hotfix_room/on_host.py --folder scripts/demos/hotfix_room/room.py \
  --folder scripts/demos/hotfix_room/namespace.yaml \
  python3 scripts/demos/hotfix_room/on_host.py up
