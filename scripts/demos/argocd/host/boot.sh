#!/usr/bin/env bash
# The cluster host's boot, run as root by release-demo-cluster-boot.service on
# every start, after a git pull. Idempotent: it installs what is missing, arms
# the idle stop first, so even a boot that fails powers the host off, starts
# k3s and Argo CD (host/cluster.py up), then the drift recorder. It writes
# /var/lib/release-demo/cluster-ready when it is done, which scripts/demo.sh
# up argocd waits for.

set -euo pipefail
exec > >(tee -a /var/log/release-demo-cluster-boot.log) 2>&1
echo "== boot $(date -u +%Y-%m-%dT%H:%M:%SZ)"

: "${REPO_DIR:?}"
HERE=$REPO_DIR/scripts/demos/argocd/host
VENV=/opt/release-demo-venv
K3S_VERSION=v1.37.1+k3s1
export AWS_DEFAULT_REGION=eu-west-1 PYTHONUNBUFFERED=1

install_once() {
  [[ -f /var/lib/release-demo/cluster-installed-v1 ]] && return
  echo "== installing packages and k3s $K3S_VERSION"
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv git jq curl openssl
  # k3s's own installer, at the pinned release; up starts it once it is configured.
  curl -sfL https://get.k3s.io |
    INSTALL_K3S_VERSION=$K3S_VERSION INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true sh -
  mkdir -p /var/lib/release-demo
  touch /var/lib/release-demo/cluster-installed-v1
}

# boto3 and PyYAML, at the versions requirements-dev.txt pins, and nothing else.
python_env() {
  [[ -x $VENV/bin/python ]] || python3 -m venv "$VENV"
  grep -E '^(boto3|PyYAML)==' "$REPO_DIR/requirements-dev.txt" > /tmp/release-demo-cluster-requirements.txt
  "$VENV/bin/pip" install -q --disable-pip-version-check -r /tmp/release-demo-cluster-requirements.txt
}

recorder() {
  cat > /etc/systemd/system/release-demo-recorder.service <<EOF
[Unit]
Description=Drift recorder: Argo CD's self-heals, from the audit log to the event bridge
After=k3s.service

[Service]
Environment=KUBECONFIG=/etc/rancher/k3s/k3s.yaml AWS_DEFAULT_REGION=eu-west-1 PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python $REPO_DIR/scripts/demos/argocd/recorder.py serve
Restart=always
RestartSec=5
EOF
  systemctl daemon-reload
  systemctl enable release-demo-recorder.service
  systemctl restart release-demo-recorder.service
}

arm_idle_stop() {
  cat > /etc/systemd/system/release-demo-idle.service <<EOF
[Unit]
Description=Stop the cluster host after 30 idle minutes

[Service]
Type=oneshot
Environment=KUBECONFIG=/etc/rancher/k3s/k3s.yaml
ExecStart=$VENV/bin/python $HERE/idle.py
EOF
  cat > /etc/systemd/system/release-demo-idle.timer <<'EOF'
[Unit]
Description=Check every 5 minutes whether the cluster host is idle

[Timer]
OnBootSec=10min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  rm -f /var/lib/release-demo/idle.json
  systemctl daemon-reload
  systemctl enable --now release-demo-idle.timer
}

rm -f /var/lib/release-demo/cluster-ready
install_once
python_env
arm_idle_stop
"$VENV/bin/python" "$HERE/cluster.py" up
recorder
date +%s > /var/lib/release-demo/cluster-ready
echo "== boot done"
