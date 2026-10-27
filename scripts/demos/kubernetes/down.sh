#!/usr/bin/env bash
# scripts/demo.sh down kubernetes: stop k3s and its pods on the tools host, so
# the cluster uses no memory. Its data stays on the data volume for the next up.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
if ! aws ec2 describe-instances --filters Name=tag:Name,Values=release-demo-tools-host \
    Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text | grep -q i-; then
  echo "The tools host is stopped, and k3s with it."
  exit 0
fi
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" python3 scripts/demos/kubernetes/host/k3s.py down
