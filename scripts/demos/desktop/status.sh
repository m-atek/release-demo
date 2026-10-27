#!/usr/bin/env bash
# scripts/demo.sh status desktop: what of demo 6 runs, and where the Jamf policy stands.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=eu-west-1

aws ec2 describe-instances --output text \
  --filters Name=tag:Demo,Values=6-desktop Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceId,State.Name]' | sed 's/^/  /'
if launchctl print "gui/$(id -u)/nl.es.release-demo.jenkins-agent" >/dev/null 2>&1; then
  echo "  Mac agent: loaded on this Mac"
else
  echo "  Mac agent: not loaded on this Mac (scripts/demos/desktop/mac_agent_setup.sh)"
fi
"$PY" "$ROOT/scripts/demos/standins/jamf.py" status --policy "Store Assistant" --bundle-id nl.es.StoreAssistant \
  --groups "Pilot,All managed Macs" 2>&1 | sed 's/^/  /' || echo "  Jamf: no answer"
