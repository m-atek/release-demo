#!/usr/bin/env bash
# scripts/demo.sh status hotfix-room: forecast's pods and the node's reading.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
if ! aws ec2 describe-instances --filters Name=tag:Name,Values=release-demo-tools-host \
    Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text | grep -q i-; then
  echo "forecast: stopped (the tools host is stopped)"
  exit 0
fi
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" \
  --folder scripts/demos/hotfix_room/on_host.py --folder scripts/demos/hotfix_room/room.py \
  python3 scripts/demos/hotfix_room/on_host.py status
