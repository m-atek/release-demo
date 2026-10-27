#!/usr/bin/env bash
# scripts/demo.sh status mobile: what of demo 7 exists and runs, and where the
# stand-ins stand. Costs: the bucket only, cents a month.

set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
[[ -x $PY ]] || PY=python3
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/release-demo

if aws ssm get-parameter --name /release-demo/store-floor/bucket --query Parameter.Value --output text >/dev/null 2>&1; then
  echo "  bucket: exists (S3, cents a month)"
else
  echo "  bucket: none (scripts/demos/mobile/tf.sh apply)"
  exit 0
fi
if pgrep -f "scripts/demos/mobile/console.py" >/dev/null; then
  echo "  console: running on http://127.0.0.1:8077"
else
  echo "  console: stopped"
fi
(cd "$ROOT" && "$PY" scripts/demos/mobile/console.py --summary 2>/dev/null | grep -v "stand-in:") || true
