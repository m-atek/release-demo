#!/usr/bin/env bash
# Terraform for demo 6's own root, infra/demos/6-desktop, with your
# short-lived AWS credentials and the platform's state bucket.
#
#   scripts/demos/desktop/tf.sh plan | apply | output | ...
#
# Uses AWS_PROFILE, by default release-demo. Sign in first: aws sso login.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
DIR=$ROOT/infra/demos/6-desktop
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
export AWS_REGION=eu-west-1

[[ $# -ge 1 ]] || { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"; exit 1; }

if [[ ! -f $DIR/.terraform/terraform.tfstate ]]; then
  bucket=$(aws ssm get-parameter --name /release-demo/terraform/state-bucket --query Parameter.Value --output text) ||
    { echo "No state bucket yet: scripts/tf.sh bootstrap"; exit 1; }
  terraform -chdir="$DIR" init -input=false -backend-config="bucket=$bucket" >/dev/null
fi
terraform -chdir="$DIR" "$@"
