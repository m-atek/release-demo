#!/usr/bin/env bash
# Run Terraform for infra/ with your short-lived AWS credentials.
#
#   scripts/tf.sh bootstrap          once: the S3 state bucket, with local state
#   scripts/tf.sh init               point infra/ at the state bucket
#   scripts/tf.sh plan | apply | ... anything else goes to terraform in infra/
#
# Uses AWS_PROFILE, by default release-demo. Sign in first: aws sso login.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
export AWS_REGION=eu-west-1

[[ $# -ge 1 ]] || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"
  exit 1
fi

if [[ $1 == bootstrap ]]; then
  terraform -chdir="$ROOT/infra/bootstrap" init -input=false
  terraform -chdir="$ROOT/infra/bootstrap" apply
  exit
fi

state_bucket() {
  aws ssm get-parameter --name /release-demo/terraform/state-bucket --query Parameter.Value --output text 2>/dev/null ||
    { echo "No state bucket yet. Run: scripts/tf.sh bootstrap" >&2; exit 1; }
}

if [[ $1 == init ]]; then
  shift
  terraform -chdir="$ROOT/infra" init -input=false -backend-config="bucket=$(state_bucket)" "$@"
  exit
fi

# .terraform/terraform.tfstate records the backend; without it, init first.
[[ -f $ROOT/infra/.terraform/terraform.tfstate ]] ||
  terraform -chdir="$ROOT/infra" init -input=false -backend-config="bucket=$(state_bucket)"
terraform -chdir="$ROOT/infra" "$@"
