#!/usr/bin/env bash
# scripts/demo.sh status kubernetes: whether k3s runs, the catalog pods with
# their versions, and the memory the cluster uses.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
if ! aws ec2 describe-instances --filters Name=tag:Name,Values=release-demo-tools-host \
    Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text | grep -q i-; then
  echo "k3s: stopped (the tools host is stopped)"
  exit 0
fi
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" python3 scripts/demos/kubernetes/host/k3s.py status
