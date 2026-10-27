#!/usr/bin/env bash
# Demo 2: the roles, the network stack, the exports, and the open pull request.
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PYTHON" "$ROOT/scripts/demos/terraform/status.py"
