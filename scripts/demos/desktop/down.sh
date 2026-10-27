#!/usr/bin/env bash
# scripts/demo.sh down desktop: stop what costs money.
#
#   scripts/demo.sh down desktop            stop the Windows instances; their disks stay
#   scripts/demo.sh down desktop --remove   remove the Windows instances too (Terraform asks first)
#
# The Jamf stand-in, a Lambda function with its state in S3, costs nothing
# while nobody calls it, so it stays.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=eu-west-1

ids=$(aws ec2 describe-instances --output text --query 'Reservations[].Instances[].InstanceId' \
  --filters Name=tag:Demo,Values=6-desktop Name=instance-state-name,Values=pending,running)
if [[ -n $ids ]]; then
  # shellcheck disable=SC2086
  aws ec2 stop-instances --instance-ids $ids >/dev/null
  echo "Stopping $ids"
else
  echo "No Windows instance of demo 6 is running."
fi
if [[ ${1:-} == --remove ]]; then
  "$HERE/tf.sh" apply -input=false -var windows=false
fi
