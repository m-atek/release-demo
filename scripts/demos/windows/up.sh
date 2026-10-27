#!/usr/bin/env bash
# scripts/demo.sh up windows: the store fleet, ready for a take.
#
# Creates the six store servers with Terraform (or starts them when they are
# stopped), waits until SSM reaches all six, keeps the fleet's read-only JCR
# token, checks that JCR holds the baseline (seeds it once), and runs the
# bootstrap: store-sync 3.2.0 on every server, the drift planted on STORE-004.
# Under 15 minutes from nothing, about 4 from stopped. Needs the tools host up.
# The fleet costs about USD 0.41 an hour while it runs; each server stops
# itself 4 hours after boot.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}
start=$SECONDS

aws sts get-caller-identity >/dev/null 2>&1 || { echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"; exit 1; }
[[ -n $(aws ec2 describe-instances --filters Name=tag:Name,Values=release-demo-tools-host Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text) ]] || { echo "The tools host is stopped. Start it first: scripts/tools.sh up"; exit 1; }

fleet() {
  aws ec2 describe-instances --output text --query 'Reservations[].Instances[].[InstanceId]' \
    --filters Name=tag:Demo,Values=1-windows Name=tag:Role,Values=store-server Name=instance-state-name,Values="$1"
}

stopped=$(fleet stopped,stopping)
if [[ -n $stopped && -z $(fleet pending,running) ]]; then
  echo "Starting the stopped fleet"
  aws ec2 wait instance-stopped --instance-ids $stopped
  aws ec2 start-instances --instance-ids $stopped >/dev/null
else
  echo "Terraform: infra/demos/1-windows"
  "$HERE/tf.sh" apply -input=false -auto-approve
  [[ -z $stopped ]] || aws ec2 start-instances --instance-ids $stopped >/dev/null
fi

ids=$(fleet pending,running)
echo "Waiting until SSM reaches all six servers"
until [[ $(aws ssm describe-instance-information --filters Key=tag:Demo,Values=1-windows \
    --query 'length(InstanceInformationList[?PingStatus==`Online`])' --output text) -ge 6 ]]; do
  (( SECONDS - start < 900 )) || { echo "The fleet doesn't answer SSM after 15 minutes."; exit 1; }
  sleep 10
done
echo "  online after $((SECONDS - start))s"

"$PY" "$HERE/jcr_token.py"
"$PY" "$HERE/seed.py"
"$PY" "$HERE/servicenow_fleet.py" >/dev/null || echo "ServiceNow: the fleet's CIs aren't checked (asleep?). Run servicenow_fleet.py before a take."
"$PY" "$HERE/grafana.py" >/dev/null || echo "Grafana: the Store fleet dashboard isn't checked. Run grafana.py before a take."
"$PY" "$HERE/reset.py" --bootstrap
echo "Ready in $((SECONDS - start))s: six store servers on 3.2.0, the drift planted on STORE-004."
