#!/usr/bin/env bash
# scripts/demo.sh up database-blip: catalog-postgres in staging and production,
# then wait until each answers and print "Ready". The cluster must be up first:
# scripts/demo.sh up kubernetes.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" --timeout 600 \
  --folder scripts/demos/database-blip/postgres.py --folder scripts/demos/database-blip/catalog-postgres.yaml \
  --folder demo-states/catalog/1.10.0/database \
  python3 scripts/demos/database-blip/postgres.py up
