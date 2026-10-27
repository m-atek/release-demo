#!/usr/bin/env bash
# scripts/demo.sh up desktop: everything demo 6 runs on, started and answering.
#
#   scripts/demo.sh up desktop          asks before Terraform changes anything
#   scripts/demo.sh up desktop --yes    doesn't ask
#
# Terraform creates what is missing in infra/demos/6-desktop (the Jamf
# stand-in, the bucket, the deploy role, the two Windows instances), the
# stopped instances start, and up waits until both answer Run Command and the
# Jamf stand-in answers its API. Then jamf_setup.py checks Jamf's groups and
# policy. The Mac agent is this Mac: scripts/demos/desktop/mac_agent_setup.sh.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=eu-west-1

approve=()
[[ ${1:-} == --yes ]] && approve=(-auto-approve)

echo "== Terraform: infra/demos/6-desktop"
"$HERE/tf.sh" apply -input=false -var windows=true ${approve[@]+"${approve[@]}"}

ids=$(aws ec2 describe-instances --output text --query 'Reservations[].Instances[].InstanceId' \
  --filters Name=tag:Demo,Values=6-desktop Name=instance-state-name,Values=pending,running,stopping,stopped)
if [[ -n $ids ]]; then
  echo "== Windows instances: $ids"
  # shellcheck disable=SC2086
  aws ec2 wait instance-stopped --instance-ids $(aws ec2 describe-instances --output text --instance-ids $ids \
    --query "Reservations[].Instances[?State.Name=='stopping'].InstanceId") 2>/dev/null || true
  # shellcheck disable=SC2086
  aws ec2 start-instances --instance-ids $ids >/dev/null
  # shellcheck disable=SC2086
  aws ec2 wait instance-running --instance-ids $ids
  start=$SECONDS
  for id in $ids; do
    printf '  %-22s' "$id"
    until [[ $(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$id" \
        --query 'InstanceInformationList[0].PingStatus' --output text) == Online ]]; do
      (( SECONDS - start < 900 )) || { echo "doesn't answer Run Command after 15 minutes"; exit 1; }
      sleep 10
    done
    echo "answers Run Command ($((SECONDS - start))s)"
  done
fi

echo "== Jamf"
"$PY" "$HERE/jamf_setup.py"
echo "Ready"
echo "  Windows build agent and test instance: USD 0.064 an hour each while running; each stops 4 hours after boot."
echo "  Jamf stand-in's page: $PY scripts/demos/standins/jamf.py ui-link"
