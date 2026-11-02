#!/usr/bin/env bash
# scripts/demo.sh status database-blip: each database's pod, READY and RESTARTS,
# its products, and whether the block is on.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
if ! aws ec2 describe-instances --filters Name=tag:Name,Values=release-demo-tools-host \
    Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text | grep -q i-; then
  echo "catalog-postgres: stopped (the tools host is stopped)"
  exit 0
fi
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" \
  --folder scripts/demos/database-blip/postgres.py \
  python3 scripts/demos/database-blip/postgres.py status
