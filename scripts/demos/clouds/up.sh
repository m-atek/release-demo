#!/usr/bin/env bash
# scripts/demo.sh up clouds: Azure's and Google Cloud's registries and apps,
# on 1.1.0, then waits until all six targets answer.
#
# For each of Azure and Google Cloud that bootstrap.sh has set up: the
# registry first, then 1.1.0 copied into it by digest from ECR, then the apps
# on that image. Terraform shows each plan and asks before it changes
# anything. AWS needs nothing: its functions cost nothing while idle.
# Uses your logins: AWS SSO, az login, gcloud auth application-default login
# and gcloud auth login.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PY:-$ROOT/.venv/bin/python}

set_up() {
  aws ssm get-parameter --name "/release-demo/clouds/$1" >/dev/null 2>&1
}

for cloud in azure gcp; do
  if ! set_up "$cloud"; then
    echo "== $cloud: not set up yet. Once its account exists: scripts/demos/clouds/bootstrap.sh $cloud"
    continue
  fi
  echo "== $cloud: registry"
  if [[ $cloud == azure ]]; then
    "$HERE/tf.sh" azure apply -var apps=true -target=azurerm_container_registry.quote -target=azurerm_role_assignment.deployer_push
  else
    "$HERE/tf.sh" gcp apply -var apps=true -target=google_artifact_registry_repository.quote -target=google_artifact_registry_repository_iam_member.deployer_push
  fi
  registry=$("$HERE/tf.sh" "$cloud" output -raw registry)
  repository=$("$HERE/tf.sh" "$cloud" output -raw repository)
  echo "== $cloud: 1.1.0 from ECR, by digest"
  "$PY" "$HERE/seed.py" "$cloud" --registry "$registry" --repository "$repository"
  echo "== $cloud: apps"
  "$HERE/tf.sh" "$cloud" apply -var apps=true
done

"$PY" "$HERE/status.py" --wait
