#!/usr/bin/env bash
# scripts/demo.sh down agent-access: nothing to stop. The role costs nothing,
# and each agent.sh container leaves when Claude Code ends.
set -euo pipefail
echo "Nothing to stop: an IAM role costs nothing, and each take's container is gone when Claude Code ends."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^release-demo-coding-agent-'; then
  echo "A take's container still runs: $(docker ps --format '{{.Names}}' | grep '^release-demo-coding-agent-' | tr '\n' ' ')"
fi
