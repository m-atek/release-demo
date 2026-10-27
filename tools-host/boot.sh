#!/usr/bin/env bash
# The tools host's boot, run as root by release-demo-boot.service on every
# start, after a git pull. Idempotent: it mounts the data volume, installs what
# is missing, puts the secrets from SSM on a tmpfs, starts the Docker stack and
# the Jenkins agent, and arms the idle stop.

set -euo pipefail
exec > >(tee -a /var/log/release-demo-boot.log) 2>&1
echo "== boot $(date -u +%Y-%m-%dT%H:%M:%SZ)"

: "${DATA_VOLUME_ID:?} ${REPO_DIR:?}"
HERE=$REPO_DIR/tools-host
VENV=/opt/release-demo-venv
SECRETS=/run/release-demo/secrets
AGENT_HOME=/var/lib/jenkins-agent
AGENT_WORK=/data/jenkins-agent
K3S_VERSION=v1.36.5+k3s1
JENKINS_PUBLIC_URL=https://jenkins-demo.es.nl/
export AWS_DEFAULT_REGION=eu-west-1

mount_data() {
  local device=/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${DATA_VOLUME_ID//-/}
  for _ in $(seq 60); do [[ -e $device ]] && break; sleep 2; done
  [[ -e $device ]] || { echo "Data volume $DATA_VOLUME_ID isn't attached"; exit 1; }
  blkid "$device" >/dev/null || mkfs.ext4 -q -L release-demo-data "$device"
  mkdir -p /data
  grep -q "LABEL=release-demo-data" /etc/fstab || echo "LABEL=release-demo-data /data ext4 defaults,nofail 0 2" >> /etc/fstab
  mountpoint -q /data || mount /data
}

install_once() {
  [[ -f /var/lib/release-demo/installed-v1 ]] && return
  echo "== installing packages"
  # Docker keeps everything on the data volume, and waits for it at boot.
  mkdir -p /etc/docker /etc/systemd/system/docker.service.d
  cat > /etc/docker/daemon.json <<'EOF'
{"data-root": "/data/docker", "log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
EOF
  printf '[Unit]\nRequiresMountsFor=/data\n' > /etc/systemd/system/docker.service.d/release-demo.conf
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    docker.io docker-compose-v2 openjdk-21-jre-headless python3-venv git jq curl
  systemctl daemon-reload
  systemctl restart docker

  id jenkins-agent >/dev/null 2>&1 ||
    useradd --system --create-home --home-dir "$AGENT_HOME" --shell /usr/sbin/nologin --groups docker jenkins-agent

  # Installed for demo 8, but not enabled or started.
  curl -sfL https://get.k3s.io |
    INSTALL_K3S_VERSION=$K3S_VERSION INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true sh -

  mkdir -p /var/lib/release-demo
  touch /var/lib/release-demo/installed-v1
}

python_env() {
  [[ -x $VENV/bin/python ]] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --disable-pip-version-check -r "$REPO_DIR/requirements-dev.txt"
}

secrets_to_tmpfs() {
  mkdir -p /run/release-demo
  mountpoint -q /run/release-demo || mount -t tmpfs -o size=16m,mode=0700 tmpfs /run/release-demo
  "$VENV/bin/python" "$HERE/host_secrets.py" "$SECRETS"
}

start_stack() {
  local splunk_env=()
  if [[ -f $SECRETS/splunk-general-terms-accepted ]]; then
    splunk_env=(SPLUNK_START_ARGS=--accept-license SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com)
  fi
  local profiles=()
  [[ -s $SECRETS/cloudflare-tunnel-token ]] && profiles=(--profile tunnel)
  local services=(jenkins jcr-db jcr)
  [[ ${#splunk_env[@]} -gt 0 ]] && services+=(splunk)
  [[ ${#profiles[@]} -gt 0 ]] && services+=(cloudflared)

  echo "== starting ${services[*]}"
  env SECRETS_DIR="$SECRETS" JENKINS_URL="$JENKINS_PUBLIC_URL" ${splunk_env[@]+"${splunk_env[@]}"} \
    docker compose -f "$HERE/docker-compose.yml" -f "$HERE/docker-compose.aws.yml" ${profiles[@]+"${profiles[@]}"} \
    up -d --build --quiet-pull "${services[@]}"
}

wait_for_jenkins() {
  for _ in $(seq 120); do
    curl -sf http://localhost:8080/login -o /dev/null && return
    sleep 5
  done
  echo "Jenkins didn't answer in 10 minutes"
  exit 1
}

start_agent() {
  echo "== Jenkins agent"
  mkdir -p "$AGENT_WORK"
  chown jenkins-agent:jenkins-agent "$AGENT_WORK"
  local admin
  admin="admin:$(cat "$SECRETS/jenkins-admin-password")"
  curl -sf -o "$AGENT_HOME/agent.jar" http://localhost:8080/jnlpJars/agent.jar
  curl -sf -u "$admin" http://localhost:8080/computer/tools-host/jenkins-agent.jnlp |
    grep -o '[0-9a-f]\{64\}' | head -1 > "$AGENT_HOME/secret"
  chown jenkins-agent:jenkins-agent "$AGENT_HOME/agent.jar" "$AGENT_HOME/secret"
  chmod 600 "$AGENT_HOME/secret"

  cat > /etc/systemd/system/jenkins-agent.service <<EOF
[Unit]
Description=Jenkins agent tools-host
After=docker.service

[Service]
User=jenkins-agent
WorkingDirectory=$AGENT_WORK
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
Environment=AWS_DEFAULT_REGION=eu-west-1
ExecStart=/usr/bin/java -jar $AGENT_HOME/agent.jar -url http://localhost:8080/ -secret @$AGENT_HOME/secret -name tools-host -webSocket -workDir $AGENT_WORK
Restart=always
RestartSec=10
EOF
  systemctl daemon-reload
  systemctl restart jenkins-agent
}

release_bot_token() {
  "$VENV/bin/python" "$HERE/release_bot_token.py" "$SECRETS" || echo "release-bot token: will retry at next boot"
}

arm_idle_stop() {
  cat > /etc/systemd/system/release-demo-idle.service <<EOF
[Unit]
Description=Stop the tools host after 30 idle minutes

[Service]
Type=oneshot
ExecStart=$VENV/bin/python $HERE/idle_stop.py $SECRETS
EOF
  cat > /etc/systemd/system/release-demo-idle.timer <<'EOF'
[Unit]
Description=Check every 5 minutes whether the tools host is idle

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

mount_data
install_once
python_env
secrets_to_tmpfs
start_stack
wait_for_jenkins
start_agent
release_bot_token
arm_idle_stop
echo "== boot done"
