#!/usr/bin/env bash
# The close-up for the video: one server's current release, its release
# folders, Tomcat and Java, refreshed every second, for a terminal at 32 px.
#
#   scripts/demos/linux/closeup.sh            inv-dc-01
#   scripts/demos/linux/closeup.sh inv-dc-02
#
# It runs /usr/local/bin/closeup on the server (installed by the baseline):
#   current  2.8.0
#   releases 2.7.3 2.8.0
#   tomcat   active since 18:02:11
#   java     1.8.0_482
# The Tomcat unit is tomcat.service. Every line stays under 44 characters.
set -euo pipefail
server=${1:-inv-dc-01}
case $server in
  inv-dc-*) exec multipass exec "$server" -- watch -t -n 1 closeup ;;
  *) echo "The close-up is for the data-center servers (inv-dc-01, inv-dc-02)." >&2; exit 2 ;;
esac
