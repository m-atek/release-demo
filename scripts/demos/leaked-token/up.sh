#!/usr/bin/env bash
# scripts/demo.sh up leaked-token: applies infra/demos/10-leaked-token, the
# host's right to write sonar/token (Terraform shows its plan and asks first).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
"$HERE/tf.sh" apply
echo "Ready. Before a take: scripts/reset.py leaked-token"
