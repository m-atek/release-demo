#!/usr/bin/env bash
# Video 25's terminal beats: each Enter shows one command, then its output, in
# the Terminal window, from services/catalog.
#
#   scripts/demos/database-blip/reveal.sh a    main on 1.10.0, after take 1: the liveness lines, then ready()
#   scripts/demos/database-blip/reveal.sh b    main on 1.10.1, before take 2: the two pairs of lines git diff prints
#
# It pulls main first, without showing it. The long values are short variables,
# set here: CHART is the chart's Deployment template, BEFORE the commit that
# staged 1.10.0, and pairs keeps the two pairs of lines, the chart's first.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
# The commands below read this when eval runs them, so the screen shows $CHART.
# shellcheck disable=SC2034
CHART=chart/templates/deployment.yaml

pairs() {
  local diff
  diff=$(cat)
  grep -E '^[-+] +httpGet: \{ path: ' <<<"$diff" | sed -E 's/^([-+]) +/\1  /'
  grep -E '^[-+] +return (ask_database\(\)|LOADED\.is_set\(\))$' <<<"$diff"
}

case ${1:-} in
  a) wanted=1.10.0
     commands=("grep -A1 livenessProbe \$CHART | sed 's/^ *//'"
               "grep -A2 'def ready' app.py | sed 's/^ *//'") ;;
  b) wanted=1.10.1
     commands=("git diff \$BEFORE -U0 -- \$CHART app.py | pairs") ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

git -C "$ROOT" pull --ff-only --quiet
cd "$ROOT/services/catalog"
# On any other version pairs finds nothing, and set -e would end this at an Enter without a word.
[[ $(cat VERSION) == "$wanted" ]] || { echo "main has catalog $(cat VERSION), not $wanted: reset.py database-blip stages it." >&2; exit 1; }
if [[ $1 == b ]]; then
  # shellcheck disable=SC2034
  BEFORE=$(git log -1 --format=%h --grep='^demo: stage catalog 1\.10\.0 ')
  [[ -n $BEFORE ]] || { echo "main has no stage of catalog 1.10.0: reset.py database-blip stages it." >&2; exit 1; }
fi
clear
for command in "${commands[@]}"; do
  read -r -s
  printf '$ %s\n' "$command"
  eval "$command"
  echo
done
read -r -s
