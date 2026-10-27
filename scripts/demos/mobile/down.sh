#!/usr/bin/env bash
# scripts/demo.sh down mobile: stops the stand-ins' console. Nothing else of
# demo 7 runs; the bucket and its state stay.

set -euo pipefail
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/release-demo
if [[ -f $STATE/mobile-console.pid ]] && kill -0 "$(cat "$STATE/mobile-console.pid")" 2>/dev/null; then
  kill "$(cat "$STATE/mobile-console.pid")"
  echo "Console stopped"
else
  echo "Console wasn't running"
fi
rm -f "$STATE/mobile-console.pid"
