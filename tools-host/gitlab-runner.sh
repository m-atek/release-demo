#!/usr/bin/env bash
# Demo 17's GitLab runner on the tools host, and release-demo-parameter. Run as
# root by boot.sh on every start; idempotent.
#
# gitlab-runner, pinned by version and SHA-256, runs as the system user
# gitlab-runner in the docker group, with the shell executor, one job at a
# time, as the project runner tools-host (its tag, locked to the project and
# refusing untagged jobs, are set on GitLab by scripts/demos/gitlab_port/setup.py).
# Jobs find the stage scripts' Python in the venv and read SSM through the
# instance role, as the Jenkins agent does. Its metrics answer on 127.0.0.1
# only, for the idle stop. It registers once, with the runner authentication
# token setup.py put in SSM. The systemd unit is installed but not enabled:
# only scripts/demo.sh up gitlab-port starts it.

set -euo pipefail

VERSION=19.5.2
SHA256=1c1752101f5b0f212615ad2c674d8f3499de96d9377f31aed1e3eecdbd068de1
BIN=/usr/local/bin/gitlab-runner
HOME_DIR=/data/gitlab-runner
CONFIG=/etc/gitlab-runner/config.toml
VENV=/opt/release-demo-venv
HERE=$(cd "$(dirname "$0")" && pwd)
export AWS_DEFAULT_REGION=eu-west-1

# The token helper jobs hand the scanner its token with, root-owned.
install -o root -g root -m 0755 "$HERE/release-demo-parameter" /usr/local/bin/release-demo-parameter

if [[ ! -x $BIN || $("$BIN" --version 2>/dev/null | awk '/^Version:/ {print $2}') != "$VERSION" ]]; then
  echo "== gitlab-runner $VERSION"
  download=$(mktemp)
  curl -fsSL -o "$download" "https://gitlab-runner-downloads.s3.amazonaws.com/v$VERSION/binaries/gitlab-runner-linux-amd64"
  if ! echo "$SHA256  $download" | sha256sum -c --quiet; then
    rm -f "$download"
    echo "gitlab-runner $VERSION: the download doesn't match its SHA-256. Not installed."
    exit 1
  fi
  install -o root -g root -m 0755 "$download" "$BIN"
  rm -f "$download"
fi

id gitlab-runner >/dev/null 2>&1 ||
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/bash --groups docker gitlab-runner
mkdir -p "$HOME_DIR/builds" "$HOME_DIR/cache/syft" "$HOME_DIR/cache/grype" "$(dirname "$CONFIG")"
chown -R gitlab-runner:gitlab-runner "$HOME_DIR"

if ! grep -qs '^\[\[runners\]\]' "$CONFIG"; then
  token=$("$VENV/bin/python" -c '
import boto3
value = boto3.client("ssm").get_parameter(Name="/release-demo/gitlab/runner-token", WithDecryption=True)["Parameter"]["Value"]
print("" if value == "unset" else value)' 2>/dev/null || true)
  if [[ -z $token ]]; then
    echo "gitlab-runner: no gitlab/runner-token in SSM yet (scripts/demos/gitlab_port/setup.py). Not registered."
  else
    # The token reaches gitlab-runner in its environment, out of ps.
    CI_SERVER_TOKEN=$token "$BIN" register --non-interactive --config "$CONFIG" --url https://gitlab.com \
      --executor shell --shell bash --name tools-host --builds-dir "$HOME_DIR/builds" --cache-dir "$HOME_DIR/cache" \
      --env "PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin" --env "AWS_DEFAULT_REGION=eu-west-1"
    echo "gitlab-runner: registered as tools-host"
  fi
  unset token
fi

if [[ -f $CONFIG ]]; then
  # One job at a time, and the metrics on localhost only.
  sed -i 's/^concurrent = .*/concurrent = 1/' "$CONFIG"
  grep -q '^listen_address' "$CONFIG" || sed -i '1i listen_address = "127.0.0.1:9252"' "$CONFIG"
  chown gitlab-runner:gitlab-runner "$CONFIG"
  chmod 600 "$CONFIG"
fi

cat > /etc/systemd/system/release-demo-gitlab-runner.service <<UNIT
[Unit]
Description=GitLab runner tools-host (demo 17), started by scripts/demo.sh up gitlab-port
After=docker.service network-online.target
Wants=network-online.target

[Service]
User=gitlab-runner
WorkingDirectory=$HOME_DIR
ExecStart=$BIN run --config $CONFIG --working-directory $HOME_DIR
Restart=on-failure
RestartSec=10
UNIT
systemctl daemon-reload
