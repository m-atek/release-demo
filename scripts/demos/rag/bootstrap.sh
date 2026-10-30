#!/usr/bin/env bash
# Demo 23, once, after you signed up for Databricks and logged in with its CLI:
#
#   scripts/demos/rag/bootstrap.sh
#
# 1. checks the Databricks CLI profile release-demo;
# 2. writes the workspace's catalog to SSM as databricks/catalog (express
#    setup names it; you confirm it);
# 3. applies infra/demos/23-databricks-rag, the plan shown first: the schema,
#    volume, service principal and its grants, the experiment, the SQL
#    warehouse, the deploy role and the SSM names;
# 4. prints where to generate the service principal's secret.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PROFILE=${DATABRICKS_CONFIG_PROFILE:-release-demo}
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=eu-west-1

databricks current-user me --profile "$PROFILE" >/dev/null 2>&1 ||
  { echo "No Databricks login. Run: databricks auth login --host <workspace URL> --profile $PROFILE"; exit 1; }
host=$(databricks auth describe --profile "$PROFILE" --output json | python3 -c "import json, sys; print(json.load(sys.stdin)['details']['host'])")
current=$(aws ssm get-parameter --name /release-demo/databricks/host --query Parameter.Value --output text 2>/dev/null || echo unset)
[[ $current != unset ]] || { echo "databricks/host isn't set yet: .venv/bin/python scripts/secrets.py set databricks/host"; exit 1; }
[[ $current == "${host%/}" || $current == "$host" ]] ||
  { echo "databricks/host in SSM isn't the workspace the profile $PROFILE signs in to."; exit 1; }

# Express setup makes one catalog for the workspace: the workspace's default namespace.
catalog=$(databricks api get /api/2.0/settings/types/default_namespace_ws/names/default --profile "$PROFILE" 2>/dev/null |
  python3 -c "import json, sys; print(json.load(sys.stdin).get('namespace', {}).get('value', ''))" 2>/dev/null || true)
if [[ -z $catalog ]]; then
  echo "The workspace's catalogs:"
  databricks catalogs list --profile "$PROFILE" --output json |
    python3 -c "import json, sys; [print('  ' + c['name']) for c in json.load(sys.stdin) if c.get('catalog_type') == 'MANAGED_CATALOG']"
fi
read -r -p "Catalog for the runbook assistant [${catalog}]: " answer
catalog=${answer:-$catalog}
[[ $catalog =~ ^[A-Za-z0-9_]+$ ]] || { echo "Not a catalog name: $catalog"; exit 1; }
aws ssm put-parameter --overwrite --type String --name /release-demo/databricks/catalog --value "$catalog" >/dev/null
echo "databricks/catalog: $catalog"

plan=$(mktemp -d)/rag.tfplan
"$ROOT/scripts/demos/rag/tf.sh" plan -input=false -out="$plan"
read -r -p "Apply this plan? Type yes: " answer
[[ $answer == yes ]] || { echo "Not applied."; exit 1; }
"$ROOT/scripts/demos/rag/tf.sh" apply -input=false "$plan"

cat <<'NEXT'

Next, about 3 minutes: the service principal's secret, shown once.
  Workspace Settings, Identity and access, Service principals, release-pipeline,
  Secrets, Generate secret. Then: .venv/bin/python scripts/secrets.py set databricks/client-secret
Then scripts/demo.sh up rag (about 20 minutes the first time) and
.venv/bin/python scripts/demos/rag/setup.py.
NEXT
