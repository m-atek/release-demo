#!/usr/bin/env bash
# Video 32's top window: production's pods, redrawn in place once a second. Real
# kubectl: NAME, READY and STATUS, or kubectl's own words when there are none.
# Every pod in the namespace, with no selector: "No resources found" has to be
# true. Needs connect.py's tunnels and certificate.
#
#   scripts/demos/one_wrong_context/pods.sh
set -uo pipefail

export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/release-demo/config}
end_of_line=$(printf '\033[K')
clear
printf '\033[?25l'
trap "printf '\033[?25h\n'" EXIT
while :; do
  frame=$(kubectl -n production get pods 2>&1)
  if [[ $frame == NAME* ]]; then
    frame=$(awk '{printf "%-30s %-6s %s\n", $1, $2, $3}' <<<"$frame")
  fi
  printf '\033[H%s\033[J' "$(sed "s/\$/$end_of_line/" <<<"Pods in production
$frame")"
  sleep 1
done
