#!/usr/bin/env bash
# Demo 2: back to the start of a take. Pass --dry-run to see what it would do.
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PYTHON" "$ROOT/scripts/demos/terraform/reset.py" "$@"
