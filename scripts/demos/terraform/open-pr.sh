#!/usr/bin/env bash
# Demo 2: push the first commit (a public bucket) and open the pull request.
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PYTHON" "$ROOT/scripts/demos/terraform/demo_pr.py" open
