#!/usr/bin/env bash
# A data-center server, inv-dc-01 or inv-dc-02. Run as root by up.sh through
# multipass exec, with dc-agent's public key on stdin: the deploy user that
# dc-agent's Ansible signs in as, with that key only and sudo without a
# password. Password sign-in stays off, as in the image. The last line is
# this server's SSH host key, which up.sh pins on dc-agent.
set -euo pipefail
key=$(cat)
[[ $key == ssh-ed25519\ * ]] || { echo "No public key on stdin" >&2; exit 1; }
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
printf '%s\n' "$key" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
echo 'deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-deploy
chmod 440 /etc/sudoers.d/90-deploy
visudo -cqf /etc/sudoers.d/90-deploy
echo "HOSTKEY $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
