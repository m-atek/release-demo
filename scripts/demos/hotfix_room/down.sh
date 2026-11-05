#!/usr/bin/env bash
# scripts/demo.sh down hotfix-room [--yes]: removes the namespace forecast and
# everything in it, after your yes, so the node has room for every other video's
# rollouts again. k3s and catalog stay: scripts/demo.sh down kubernetes stops those.
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
  read -r -p "Remove the namespace forecast and everything in it? Type yes: " answer
  [[ $answer == yes ]] || { echo "Nothing removed."; exit 1; }
fi
exec "$PY" "$ROOT/scripts/demos/kubernetes/host.py" --timeout 300 \
  --folder scripts/demos/hotfix_room/on_host.py --folder scripts/demos/hotfix_room/room.py \
  python3 scripts/demos/hotfix_room/on_host.py down
