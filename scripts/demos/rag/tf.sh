#!/usr/bin/env bash
# Terraform for demo 23's own root, infra/demos/23-databricks-rag, with your
# short-lived AWS credentials and your Databricks login, the CLI profile
# release-demo (databricks auth login --profile release-demo), as demo 5 uses
# az and gcloud. Its state sits in the bucket of infra/, under the key
# release-demo/demos/23-databricks-rag.tfstate.
#
#   scripts/demos/rag/tf.sh init                 point the root at the state bucket
#   scripts/demos/rag/tf.sh plan | apply | ...   anything else goes to terraform

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
DIR=$ROOT/infra/demos/23-databricks-rag
export AWS_PROFILE=${AWS_PROFILE:-release-demo}
export AWS_REGION=eu-west-1
export DATABRICKS_CONFIG_PROFILE=${DATABRICKS_CONFIG_PROFILE:-release-demo}

[[ $# -ge 1 ]] || { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"
  exit 1
fi
if ! databricks current-user me --profile "$DATABRICKS_CONFIG_PROFILE" >/dev/null 2>&1; then
  echo "Not signed in to Databricks. Run: databricks auth login --host <workspace URL> --profile $DATABRICKS_CONFIG_PROFILE"
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
