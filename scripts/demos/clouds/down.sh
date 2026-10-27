#!/usr/bin/env bash
# scripts/demo.sh down clouds: deletes Azure's and Google Cloud's registries
# and apps, so nothing is billed overnight. The Azure registry is billed per
# day while it exists. Their trust in Jenkins, the identities and the budget
# alerts stay, so up brings everything back on 1.1.0. Terraform shows what it
# deletes and asks first. AWS stays as it is: idle Lambda functions cost nothing.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PY:-$ROOT/.venv/bin/python}

for cloud in azure gcp; do
  if aws ssm get-parameter --name "/release-demo/clouds/$cloud" >/dev/null 2>&1; then
    echo "== $cloud: deleting the registry and apps"
    "$HERE/tf.sh" "$cloud" apply -var apps=false
  fi
done

"$PY" "$HERE/status.py"
