#!/usr/bin/env bash
# Package the approval bridge for Lambda: handler.py and the packages in
# requirements.txt, installed for Lambda's platform (Python 3.13 on x86_64,
# manylinux wheels only) into infra/.build/approval-bridge/, which
# infra/approval_bridge.tf zips. scripts/tf.sh runs it before Terraform reads
# infra/; it does nothing while the folder holds this handler and these
# requirements already.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$ROOT/infra/.build/approval-bridge
STAMP=$ROOT/infra/.build/approval-bridge.inputs
PY=$ROOT/.venv/bin/python
[[ -x $PY ]] || PY=python3

inputs=$(cat "$ROOT/bridge/handler.py" "$ROOT/bridge/requirements.txt" | shasum -a 256 | cut -c1-16)
[[ -d $OUT && -f $STAMP && $(cat "$STAMP") == "$inputs" ]] && exit 0

echo "Packaging the approval bridge for Lambda"
rm -rf "${OUT:?}" && mkdir -p "$OUT"
"$PY" -m pip install --quiet --disable-pip-version-check --no-compile --target "$OUT" \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --implementation cp --python-version 3.13 \
  --only-binary=:all: --requirement "$ROOT/bridge/requirements.txt"
rm -rf "${OUT:?}/bin"
cp "$ROOT/bridge/handler.py" "$OUT/handler.py"
echo "$inputs" > "$STAMP"
