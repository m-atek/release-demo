#!/usr/bin/env bash
# Start, stop and check one demo's own infrastructure, so it costs nothing
# while nobody records or demos it. The shared platform is scripts/tools.sh.
#
#   scripts/demo.sh up <demo>       create or start it, wait until it answers
#   scripts/demo.sh down <demo>     stop it
#   scripts/demo.sh status [<demo>] what runs, for one demo or all of them
#
# Each demo implements these in scripts/demos/<demo>/{up,down,status}.sh.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEMOS=$ROOT/scripts/demos

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  echo "Demos: $(cd "$DEMOS" 2>/dev/null && ls -d */ 2>/dev/null | tr -d / | tr '\n' ' ')"
  exit 2
}

[[ $# -ge 1 ]] || usage
action=$1 demo=${2:-}
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}

case $action in
  up|down)
    [[ -n $demo && -x $DEMOS/$demo/$action.sh ]] || usage
    exec "$DEMOS/$demo/$action.sh"
    ;;
  status)
    if [[ -n $demo ]]; then
      exec "$DEMOS/$demo/status.sh"
    fi
    for script in "$DEMOS"/*/status.sh; do
      [[ -x $script ]] || continue
      echo "== $(basename "$(dirname "$script")")"
      "$script" || true
    done
    ;;
  *) usage ;;
esac
