#!/usr/bin/env bash
# scripts/demo.sh status linux: see scripts/demos/linux/demo.py.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
exec "${PYTHON:-$ROOT/.venv/bin/python}" "$ROOT/scripts/demos/linux/demo.py" status "$@"
