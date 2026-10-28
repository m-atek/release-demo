#!/usr/bin/env bash
# scripts/demo.sh up mobile: what demo 7 needs on this Mac for a take or a
# call. Nothing of its own costs money by the hour: the bucket is a few cents
# a month, the stand-ins are state in it. This checks the bucket, gives the
# stand-ins their starting state if they have none, and starts the console
# with the stand-ins' pages on http://127.0.0.1:8077. The tools host and the
# Mac agent are the platform's and demo 6's: scripts/tools.sh up starts the host.

set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/release-demo
mkdir -p "$STATE"

aws ssm get-parameter --name /release-demo/store-floor/bucket --query Parameter.Value --output text >/dev/null 2>&1 ||
  { echo "No store-floor bucket yet. Apply demo 7's root: scripts/demos/mobile/tf.sh apply"; exit 1; }

(cd "$ROOT" && "$PY" - <<'PY'
import sys
sys.path.insert(0, "scripts/demos/mobile")
import jamf_apps, mobile, reset
store = mobile.state_store()
if not store.read("testflight.json"):
    reset.initial_standins(store)
    print("Stand-ins: created, everything on 2.3.0")
# Demo 6's Jamf stand-in keeps its own state: Store Floor's app in it, if missing.
client = jamf_apps.jamf()
if isinstance(client, jamf_apps.SharedJamf):
    config = mobile.config()
    client.ensure_app(config["jamf"]["app"], config["app"]["bundle_id"], config["jamf"]["pilot_group"])
PY
)

if pgrep -f "scripts/demos/mobile/console.py" >/dev/null; then
  echo "Console already running: http://127.0.0.1:8077"
else
  (cd "$ROOT" && nohup "$PY" scripts/demos/mobile/console.py > "$STATE/mobile-console.log" 2>&1 &)
  for _ in $(seq 1 30); do
    curl -sf -o /dev/null http://127.0.0.1:8077/jamf && break
    sleep 1
  done
  curl -sf -o /dev/null http://127.0.0.1:8077/jamf || { echo "The console didn't start: $STATE/mobile-console.log"; exit 1; }
  head -1 "$STATE/mobile-console.log"
fi
echo "Ready: http://127.0.0.1:8077/jamf, /testflight and /play"
