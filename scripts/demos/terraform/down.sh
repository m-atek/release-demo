#!/usr/bin/env bash
# Demo 2 has nothing to stop: the network stack has no instances or NAT
# gateway, and the bucket and queue cost cents a month while they exist.
# reset.py terraform removes those and puts the stack back on its baseline.
set -euo pipefail
echo "Demo 2 runs nothing by the hour. To remove the exports bucket and queue: scripts/demos/terraform/reset.py"
