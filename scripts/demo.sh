#!/usr/bin/env bash
# Start, stop and check one demo's own infrastructure, so it costs nothing
# while nobody records or demos it. The shared platform is scripts/tools.sh.
#
#   scripts/demo.sh up <demo>       create or start it, wait until it answers
#   scripts/demo.sh down <demo>     stop it
#   scripts/demo.sh status [<demo>] what runs, for one demo or all of them
#   scripts/demo.sh <action> <demo> any other step a demo has, for example open-pr
#   Anything after the demo goes to its script: scripts/demo.sh up desktop --yes
#
# Each demo implements these in scripts/demos/<demo>/<action>.sh.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEMOS=$ROOT/scripts/demos

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  echo "Demos: $(cd "$DEMOS" 2>/dev/null && for d in */; do [[ -d $d ]] && printf '%s ' "${d%/}"; done)"
  exit 2
}

[[ $# -ge 1 ]] || usage
action=$1 demo=${2:-}
# cost-gate is scripts/demos/cost_gate, gitlab-port scripts/demos/gitlab_port: without a folder of its own
# name, a dash is an underscore. agent-access and rollback-trap keep their dashes.
[[ -z $demo || -d $DEMOS/$demo ]] || demo=${demo//-/_}
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}

case $action in
  up|down)
    [[ -n $demo && -x $DEMOS/$demo/$action.sh ]] || usage
    exec "$DEMOS/$demo/$action.sh" "${@:3}"
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
  *)
    [[ -n $demo && -x $DEMOS/$demo/$action.sh ]] || usage
    exec "$DEMOS/$demo/$action.sh" "${@:3}"
    ;;
esac
