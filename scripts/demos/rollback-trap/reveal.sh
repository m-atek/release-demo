#!/usr/bin/env bash
# Video 20's terminal beats: each Enter shows one command, then its output,
# in the 56-column Terminal window, from services/orders-service.
#
#   scripts/demos/rollback-trap/reveal.sh a    after take A: the rename, the query it missed, 1.6.0's queries, the log line
#   scripts/demos/rollback-trap/reveal.sh c    before take C: 1.6.2's migration
#   scripts/demos/rollback-trap/reveal.sh d    before take D: 1.7.0's migration
#
# It pulls main and the tags first, without showing it. The long values are
# short variables, set here: LOG is production's log group, ERRORS the last
# 30 minutes of lines with "database" in them, without their timestamps.
# Uses AWS_PROFILE, by default release-demo, for the log.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}
# The commands below read these when eval runs them, so the screen shows $TAG.
# shellcheck disable=SC2034
{
  TAG=orders-service-v1.6.0
  LOG=/aws/lambda/orders-service-production
  ERRORS="--since 30m --format short --filter-pattern database"
}

case ${1:-} in
  a) commands=("cat migrations/0003_rename_delivery_days.sql"
               "git grep -n delivery_days -- handler.py"
               "git show \$TAG:./handler.py | grep -n delivery_days"
               "aws logs tail \$LOG \$ERRORS | cut -c21-") ;;
  c) commands=("cat migrations/0003_rename_delivery_days.sql") ;;
  d) commands=("cat migrations/0003_add_delivery_estimate_days.sql") ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

git -C "$ROOT" pull --ff-only --quiet
git -C "$ROOT" fetch --tags --quiet
cd "$ROOT/services/orders-service"
clear
for command in "${commands[@]}"; do
  read -r -s
  printf '$ %s\n' "$command"
  eval "$command"
  echo
done
read -r -s
