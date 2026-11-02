#!/usr/bin/env bash
# Video 25's top window: staging's pods, redrawn in place once a second. Real
# kubectl output: POD, READY, RESTARTS. Needs connect.py's tunnels and certificate.
#
#   scripts/demos/database-blip/pods.sh
set -uo pipefail

export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/release-demo/config}
COLUMNS_SPEC='POD:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount'
end_of_line=$(printf '\033[K')
clear
printf '\033[?25l'
trap "printf '\033[?25h\n'" EXIT
while :; do
  frame=$(kubectl -n staging get pods -o "custom-columns=$COLUMNS_SPEC" 2>&1)
  printf '\033[H%s\033[J' "$(sed "s/\$/$end_of_line/" <<<"$frame")"
  sleep 1
done
