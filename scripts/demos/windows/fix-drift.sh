#!/usr/bin/env bash
# scripts/demo.sh fix-drift windows: restore UploadEndpoint on STORE-004,
# before /release store-sync ORD-31 resume. reset.py plants the drift again.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}
exec "${PYTHON:-$ROOT/.venv/bin/python}" "$HERE/reset.py" --fix-drift
