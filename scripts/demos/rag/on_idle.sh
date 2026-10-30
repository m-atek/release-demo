#!/usr/bin/env bash
# Run by the tools host's idle stop (tools-host/idle_stop.py) just before it
# powers the host off: deletes demo 23's search endpoint and its indexes, as
# scripts/demo.sh down rag does, so a forgotten endpoint can't bill all night
# (decision 45). As the service principal, which up made manager of the
# endpoint. Off when services.yaml says runbook-assistant's
# databricks.down_on_idle: false. Never stops the power-off: it only logs.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY=${PYTHON:-/opt/release-demo-venv/bin/python}
[[ -x $PY ]] || PY=python3
enabled=$("$PY" -c "import sys; sys.path.insert(0, '$ROOT/scripts'); from common import service; print(service('runbook-assistant')['databricks'].get('down_on_idle', True))" 2>/dev/null)
[[ $enabled == True ]] || { echo "rag: down_on_idle is off"; exit 0; }
timeout 300 "$PY" "$ROOT/scripts/demos/rag/search.py" down --as pipeline || echo "rag: the endpoint wasn't deleted; scripts/demo.sh status rag shows it"
