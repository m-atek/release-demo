#!/usr/bin/env bash
# Demo 2: push the second commit, which makes the bucket private.
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PYTHON" "$ROOT/scripts/demos/terraform/demo_pr.py" fix
