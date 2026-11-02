#!/usr/bin/env bash
# scripts/demo.sh down database-blip [--yes]: removes catalog-postgres, its data
# and its Secret from staging and production, by their label, after your yes.
# Others' objects stay, and so do k3s and catalog: scripts/demo.sh down kubernetes
# stops those.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
if ! aws ec2 describe-instances --filters Name=tag:Name,Values=release-demo-tools-host \
    Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text | grep -q i-; then
  echo "The tools host is stopped: nothing to remove now."
  exit 0
fi
if [[ ${1:-} != --yes ]]; then
  read -r -p "Remove catalog-postgres, its data and its Secret from staging and production? Type yes: " answer
  [[ $answer == yes ]] || { echo "Nothing removed."; exit 1; }
fi
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" \
  --folder scripts/demos/database-blip/postgres.py \
  python3 scripts/demos/database-blip/postgres.py down
