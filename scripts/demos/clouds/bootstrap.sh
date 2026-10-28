#!/usr/bin/env bash
# One-time setup of each cloud's trust in Jenkins, with your own short-lived login.
#
#   scripts/demos/clouds/bootstrap.sh aws     the Jenkins issuer bucket, AWS's trust in it, ECR with 1.1.0, both functions
#   scripts/demos/clouds/bootstrap.sh azure   the deploy identity with its federated credential
#   scripts/demos/clouds/bootstrap.sh gcp     the APIs, the workload identity pool and provider, the deploy service account
#
# After that the pipeline needs no login of yours: each cloud trusts the
# build's ID token. Azure's and Google Cloud's registries and apps come and go
# with scripts/demo.sh up clouds and down clouds. Terraform shows each plan
# and asks before it changes anything.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PY:-$ROOT/.venv/bin/python}

case ${1:-} in
  aws)
    # The functions run the 1.1.0 image, so ECR and the image come first.
    "$HERE/tf.sh" aws apply -target=aws_ecr_repository.quote -target=aws_ecr_lifecycle_policy.quote
    "$PY" "$HERE/seed.py" aws
    "$HERE/tf.sh" aws apply
    echo
    echo "AWS trusts the Jenkins issuer. Once Jenkins has its ID token credentials (casc.yaml),"
    echo "the tools host publishes their keys at boot; to publish them now, on the host:"
    echo "  sudo /opt/release-demo-venv/bin/python scripts/demos/clouds/oidc_issuer.py publish"
    ;;
  azure | gcp)
    if [[ $1 == gcp ]]; then
      # Reading the project needs the Resource Manager API, which this root
      # turns on: on a new project the APIs go first, or the plan stops.
      "$HERE/tf.sh" gcp apply -var apps=false -target=google_project_service.apis
    fi
    "$HERE/tf.sh" "$1" apply -var apps=false
    echo
    echo "$1 trusts the Jenkins issuer. Its registry and apps: scripts/demo.sh up clouds"
    ;;
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
