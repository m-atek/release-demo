#!/usr/bin/env bash
# Video 33's terminal beat: Enter shows the command, then its output, from
# services/forecast: the chart's reservation before and after, nothing else.
#
#   scripts/demos/hotfix_room/reveal.sh
#
# It pulls main first, without showing it. The long values are short variables,
# set here: VALUES is the chart's values file, BEFORE the commit before 2.4.1 was
# staged, and memory keeps the four lines.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
# The command below reads this when eval runs it, so the screen shows $VALUES.
# shellcheck disable=SC2034
VALUES=chart/values.yaml

memory() {
  grep -E '^[-+] +(requests|limits): '
}

git -C "$ROOT" pull --ff-only --quiet
cd "$ROOT/services/forecast"
# On any other version memory finds nothing, and set -e would end this at the Enter without a word.
[[ $(cat VERSION) == 2.4.1 ]] || { echo "main has forecast $(cat VERSION), not 2.4.1: reset.py hotfix-room stages it." >&2; exit 1; }
# shellcheck disable=SC2034
BEFORE=$(git log -1 --format=%h --grep='^demo: stage forecast 2\.4\.1 ')~1
[[ $BEFORE != "~1" ]] || { echo "main has no stage of forecast 2.4.1: reset.py hotfix-room stages it." >&2; exit 1; }
command='git diff $BEFORE -U0 -- $VALUES | memory'
clear
read -r -s
printf '$ %s\n' "$command"
eval "$command"
echo
read -r -s
