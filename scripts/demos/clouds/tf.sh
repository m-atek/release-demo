#!/usr/bin/env bash
# Run Terraform for demo 5's roots with your short-lived logins.
#
#   scripts/demos/clouds/tf.sh aws plan|apply|...     infra/demos/5-three-clouds: AWS and the Jenkins issuer
#   scripts/demos/clouds/tf.sh azure plan|apply|...   infra/demos/5-three-clouds/azure (az login first)
#   scripts/demos/clouds/tf.sh gcp plan|apply|...     infra/demos/5-three-clouds/gcp (gcloud auth application-default login first)
#
# Each root keeps its own state in the state bucket. The Azure subscription and
# the Google Cloud project come from SSM (scripts/secrets.py set azure, set gcp),
# and the budget email from infra/local.auto.tfvars, as for infra/.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
export AWS_REGION=eu-west-1

[[ $# -ge 2 ]] || { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
cloud=$1
shift
case $cloud in
  aws) dir=$ROOT/infra/demos/5-three-clouds key=release-demo/demos/5-three-clouds.tfstate ;;
  azure | gcp) dir=$ROOT/infra/demos/5-three-clouds/$cloud key=release-demo/demos/5-three-clouds-$cloud.tfstate ;;
  *) echo "Unknown cloud $cloud: aws, azure or gcp"; exit 2 ;;
esac

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"
  exit 1
fi

parameter() {
  local value
  value=$(aws ssm get-parameter --name "/release-demo/$1" --query Parameter.Value --output text 2>/dev/null || true)
  [[ -n $value && $value != unset ]] || { echo "/release-demo/$1 isn't set. Run: .venv/bin/python scripts/secrets.py set ${1%%/*}" >&2; exit 1; }
  printf '%s' "$value"
}

case $cloud in
  azure)
    az account show >/dev/null 2>&1 || { echo "Not signed in to Azure. Run: az login"; exit 1; }
    ARM_SUBSCRIPTION_ID=$(parameter azure/subscription-id)
    export ARM_SUBSCRIPTION_ID
    ;;
  gcp)
    gcloud auth application-default print-access-token >/dev/null 2>&1 ||
      { echo "No Google Cloud login for Terraform. Run: gcloud auth application-default login"; exit 1; }
    TF_VAR_project_id=$(parameter gcp/project-id)
    export TF_VAR_project_id
    ;;
esac

state_bucket=$(aws ssm get-parameter --name /release-demo/terraform/state-bucket --query Parameter.Value --output text)

# The budget email, as infra/ has it. Terraform warns about the file's other values.
var_file=()
[[ -f $ROOT/infra/local.auto.tfvars && $1 =~ ^(plan|apply|destroy|import|refresh|console)$ ]] &&
  var_file=(-var-file="$ROOT/infra/local.auto.tfvars")

[[ -f $dir/.terraform/terraform.tfstate ]] ||
  terraform -chdir="$dir" init -input=false -backend-config="bucket=$state_bucket" -backend-config="key=$key"
terraform -chdir="$dir" "$@" ${var_file[@]+"${var_file[@]}"}
