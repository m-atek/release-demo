#!/usr/bin/env bash
# scripts/demo.sh status clouds: each target's version and what runs.
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
exec "${PY:-$ROOT/.venv/bin/python}" "$HERE/status.py"
