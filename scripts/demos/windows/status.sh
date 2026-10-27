#!/usr/bin/env bash
# scripts/demo.sh status windows: the store fleet, what each server runs and what it costs.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}

aws sts get-caller-identity >/dev/null 2>&1 || { echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"; exit 1; }

cd "$ROOT"
"$PY" - <<'EOF'
import sys
sys.path.insert(0, "scripts"); sys.path.insert(0, "scripts/demos/windows")
from common import service
from fleet import Fleet
fleet = Fleet(service("store-sync"))
instances = fleet.instances()
if not instances:
    print("No store fleet. Create it: scripts/demo.sh up windows")
    sys.exit(0)
running = {n: i["id"] for n, i in instances.items() if i["state"] == "running"}
answers = fleet.state(running, "store-sync: status of the fleet") if running else {}
for name, instance in instances.items():
    line = f"  {name}  {instance['id']}  {instance['state']}"
    answer = answers.get(name)
    if answer and answer.ok:
        r = answer.result
        drift = "" if "UploadEndpoint" in r.get("config_keys", []) else ", no UploadEndpoint (drift)"
        line += f"  store-sync {r.get('version')}, /health {r.get('health_code')}{drift}"
    print(line)
cost = 0.069 * len(running)
print(f"Running: {len(running)} of {len(instances)}, about USD {cost:.2f} an hour" if running
      else "All stopped: only the disks cost, about USD 0.52 a day.")
EOF
