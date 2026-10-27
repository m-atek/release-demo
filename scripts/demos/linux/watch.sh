#!/usr/bin/env bash
# The four servers in a tmux window, two by two, each pane showing that
# server's /health in large type every second: the cloud servers through
# Session Manager, the data-center servers through multipass exec. Each pane
# runs /opt/inventory/bin/inventory-watch on its server, which asks Tomcat on
# 127.0.0.1, so no port is open anywhere. Needs tmux and the Session Manager
# plugin: brew install tmux; brew install --cask session-manager-plugin.
#
#   scripts/watch_inventory.sh        then Ctrl-b d to leave, or tmux kill-session -t inventory
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}
SESSION=inventory
WATCH=/opt/inventory/bin/inventory-watch

command -v tmux >/dev/null || { echo "Needs tmux: brew install tmux"; exit 1; }
command -v session-manager-plugin >/dev/null ||
  { echo "Needs the Session Manager plugin: brew install --cask session-manager-plugin"; exit 1; }

instance() {
  aws ec2 describe-instances --output text --query 'Reservations[0].Instances[0].InstanceId' \
    --filters "Name=tag:Name,Values=$1" Name=tag:Demo,Values=3-linux Name=instance-state-name,Values=running
}

pane() {
  case $1 in
    inv-cloud-*)
      local id
      id=$(instance "$1")
      [[ $id == i-* ]] || { echo "echo '$1 is not running: scripts/demo.sh up linux'; sleep 3600"; return; }
      echo "aws ssm start-session --target $id --document-name AWS-StartInteractiveCommand --parameters command='$WATCH'" ;;
    *) echo "multipass exec $1 -- $WATCH" ;;
  esac
}

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -x 160 -y 50 "while true; do $(pane inv-cloud-01); sleep 2; done"
tmux split-window -h -t "$SESSION" "while true; do $(pane inv-cloud-02); sleep 2; done"
tmux split-window -v -t "$SESSION:0.0" "while true; do $(pane inv-dc-01); sleep 2; done"
tmux split-window -v -t "$SESSION:0.2" "while true; do $(pane inv-dc-02); sleep 2; done"
tmux set -t "$SESSION" pane-border-status top
tmux set -t "$SESSION" status off
titles=("inv-cloud-01 · AWS" "inv-dc-01 · Data center" "inv-cloud-02 · AWS" "inv-dc-02 · Data center")
for i in 0 1 2 3; do tmux select-pane -t "$SESSION:0.$i" -T "${titles[$i]}"; done
exec tmux attach -t "$SESSION"
