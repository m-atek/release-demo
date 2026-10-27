#!/usr/bin/env bash
# Demo 2: the pipeline's two roles and the network stack's baseline. Neither
# runs by the hour, so up creates them once and after that only checks: with
# nothing to change, Terraform says so and asks nothing. The pull requests run
# on Jenkins, so the tools host must be up too: scripts/tools.sh up.
set -euo pipefail
source "$(dirname "$0")/_python.sh"
"$PYTHON" "$ROOT/scripts/demos/terraform/stack.py" up
"$PYTHON" "$ROOT/scripts/demos/terraform/status.py"
