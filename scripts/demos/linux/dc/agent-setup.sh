#!/usr/bin/env bash
# dc-agent, the data center's Jenkins agent. Run as root by up.sh through
# multipass exec on every start, from /opt/dc-setup. Idempotent. Installs Java
# for the agent, Python and Ansible for the rollout, the jenkins user and its
# SSH key, and the agent's systemd service, which connects out to Jenkins over
# WebSocket. Nothing listens here. The last line is the jenkins user's public
# key, for the data-center servers.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
HERE=$(cd "$(dirname "$0")" && pwd)
HOME_DIR=/var/lib/jenkins-agent

need=()
for package in openjdk-21-jre-headless python3-venv python3-yaml git curl; do
  dpkg -s "$package" >/dev/null 2>&1 || need+=("$package")
done
if (( ${#need[@]} )); then
  apt-get update -q >/dev/null
  apt-get install -y -q --no-install-recommends "${need[@]}" >/dev/null
  echo "installed ${need[*]}"
fi

id jenkins >/dev/null 2>&1 ||
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/bash jenkins
install -d -m 700 -o jenkins -g jenkins "$HOME_DIR/.ssh"
[[ -f $HOME_DIR/.ssh/id_ed25519 ]] ||
  sudo -u jenkins ssh-keygen -q -t ed25519 -N '' -C jenkins@dc-agent -f "$HOME_DIR/.ssh/id_ed25519"

# Ansible in its own virtualenv, rebuilt when the pinned versions change.
wanted=$(sha256sum "$HERE/requirements.txt" | cut -c1-64)
if [[ $(cat /opt/ansible/.requirements.sha256 2>/dev/null) != "$wanted" ]]; then
  rm -rf /opt/ansible
  python3 -m venv /opt/ansible
  /opt/ansible/bin/pip install -q --disable-pip-version-check -r "$HERE/requirements.txt"
  echo "$wanted" > /opt/ansible/.requirements.sha256
  echo "installed $(/opt/ansible/bin/ansible --version | head -1)"
fi

install -m 755 "$HERE/jenkins-agent-start" /usr/local/bin/jenkins-agent-start
install -m 644 "$HERE/jenkins-agent.service" /etc/systemd/system/jenkins-agent.service
install -d -m 700 -o root -g root /etc/jenkins-agent
systemctl daemon-reload
systemctl enable jenkins-agent >/dev/null 2>&1

echo "PUBKEY $(cat "$HOME_DIR/.ssh/id_ed25519.pub")"
