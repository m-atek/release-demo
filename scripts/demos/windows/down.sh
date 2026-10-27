#!/usr/bin/env bash
# scripts/demo.sh down windows: stop the store fleet.
#
#   scripts/demos/windows/down.sh             stop the six servers: only their disks cost, about USD 0.52 a day
#   scripts/demos/windows/down.sh --destroy   remove the whole fleet with Terraform; the next up builds it anew
#
# Stopping keeps the servers as they are, so the next up takes about 4
# minutes. --destroy asks Terraform's own confirmation first.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}

aws sts get-caller-identity >/dev/null 2>&1 || { echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"; exit 1; }

if [[ ${1:-} == --destroy ]]; then
  "$HERE/tf.sh" destroy
  exit
fi

ids=$(aws ec2 describe-instances --output text --query 'Reservations[].Instances[].InstanceId' \
  --filters Name=tag:Demo,Values=1-windows Name=tag:Role,Values=store-server Name=instance-state-name,Values=pending,running)
if [[ -z $ids ]]; then
  echo "The store fleet isn't running."
  exit 0
fi
aws ec2 stop-instances --instance-ids $ids >/dev/null
echo "Stopping $(wc -w <<<"$ids" | tr -d ' ') store servers. Their disks stay: about USD 0.52 a day. down.sh --destroy removes them."
