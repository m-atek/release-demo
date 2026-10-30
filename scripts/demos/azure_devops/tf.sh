#!/usr/bin/env bash
# Terraform for demo 22's own root, infra/demos/22-azure-devops-approval, with
# your short-lived logins: AWS for the account setting, the roles' policies and
# the state, and az login for Entra, in the es.nl tenant. Its state sits in the
# bucket of infra/, under the key release-demo/demos/22-azure-devops-approval.tfstate.
#
#   scripts/demos/azure_devops/tf.sh init                 point the root at the state bucket
#   scripts/demos/azure_devops/tf.sh plan | apply | ...   anything else goes to terraform
#
# Uses AWS_PROFILE, by default release-demo. Sign in first: aws sso login, az login.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
DIR=$ROOT/infra/demos/22-azure-devops-approval
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
export AWS_REGION=eu-west-1

[[ $# -ge 1 ]] || { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"
  exit 1
fi
az account show >/dev/null 2>&1 || { echo "Not signed in to Azure. Run: az login"; exit 1; }

state_bucket() {
  aws ssm get-parameter --name /release-demo/terraform/state-bucket --query Parameter.Value --output text 2>/dev/null ||
    { echo "No state bucket yet. Run: scripts/tf.sh bootstrap" >&2; exit 1; }
}

if [[ $1 == init ]]; then
  shift
  terraform -chdir="$DIR" init -input=false -backend-config="bucket=$(state_bucket)" "$@"
  exit
fi

# .terraform/terraform.tfstate records the backend; without it, init first.
[[ -f ${TF_DATA_DIR:-$DIR/.terraform}/terraform.tfstate ]] ||
  terraform -chdir="$DIR" init -input=false -backend-config="bucket=$(state_bucket)"
terraform -chdir="$DIR" "$@"
