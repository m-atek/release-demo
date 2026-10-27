#!/usr/bin/env bash
# Run Terraform for demo 7's own root, infra/demos/7-mobile, with your
# short-lived AWS credentials, as scripts/tf.sh does for infra/.
#
#   scripts/demos/mobile/tf.sh init               point it at the state bucket
#   scripts/demos/mobile/tf.sh plan | apply | ...  anything else goes to terraform
#
# Uses AWS_PROFILE, by default release-demo. Sign in first: aws sso login.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
DIR=$ROOT/infra/demos/7-mobile
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
export AWS_REGION=eu-west-1

[[ $# -ge 1 ]] || { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"
  exit 1
fi

state_bucket() {
  aws ssm get-parameter --name /release-demo/terraform/state-bucket --query Parameter.Value --output text 2>/dev/null ||
    { echo "No state bucket yet. Run: scripts/tf.sh bootstrap" >&2; exit 1; }
}

if [[ $1 == init ]]; then
  shift
  terraform -chdir="$DIR" init -input=false -backend-config="bucket=$(state_bucket)" "$@"
  exit
fi

[[ -f $DIR/.terraform/terraform.tfstate ]] ||
  terraform -chdir="$DIR" init -input=false -backend-config="bucket=$(state_bucket)"
terraform -chdir="$DIR" "$@"
