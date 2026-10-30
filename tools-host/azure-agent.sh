#!/usr/bin/env bash
# Demo 22's Azure Pipelines agent on the tools host. Run as root by boot.sh on
# every start; idempotent.
#
# The agent, pinned by version and SHA-256, runs as the system user azp-agent
# in the docker group, in the pool release-demo, named tools-host, one job at a
# time. Jobs find the venv's Python and read SSM through the instance role, as
# the Jenkins agent does; release-demo-parameter (gitlab-runner.sh installs it)
# hands the scanner its token. It registers once, with the personal access
# token you typed (azure-devops/agent-registration-token, scope Agent Pools,
# a day's life); after that it signs in with its own key. The systemd unit is
# installed but not enabled: only scripts/demo.sh up azure-devops starts it.

set -euo pipefail

VERSION=5.281.0
SHA256=a8cd0f5371f3c6fb8f677ed89b0c03f191dbdf1664920ad9bb288421b90cf2bb
HOME_DIR=/data/azp-agent
AGENT_DIR=$HOME_DIR/agent
VENV=/opt/release-demo-venv
export AWS_DEFAULT_REGION=eu-west-1

id azp-agent >/dev/null 2>&1 ||
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/bash --groups docker azp-agent
mkdir -p "$AGENT_DIR" "$HOME_DIR/_work" "$HOME_DIR/cache/syft" "$HOME_DIR/cache/grype"

if [[ $(cat "$AGENT_DIR/.release-demo-version" 2>/dev/null) != "$VERSION" ]]; then
  echo "== Azure Pipelines agent $VERSION"
  download=$(mktemp)
  curl -fsSL -o "$download" "https://download.agent.dev.azure.com/agent/$VERSION/vsts-agent-linux-x64-$VERSION.tar.gz"
  if ! echo "$SHA256  $download" | sha256sum -c --quiet; then
    rm -f "$download"
    echo "Azure Pipelines agent $VERSION: the download doesn't match its SHA-256. Not installed."
    exit 1
  fi
  tar -xzf "$download" -C "$AGENT_DIR"
  rm -f "$download"
  "$AGENT_DIR/bin/installdependencies.sh" >/dev/null
  echo "$VERSION" > "$AGENT_DIR/.release-demo-version"
fi

# What the agent hands its jobs: the venv first on the PATH, and the region.
echo "$VENV/bin:/usr/local/bin:/usr/bin:/bin" > "$AGENT_DIR/.path"
echo "AWS_DEFAULT_REGION=eu-west-1" > "$AGENT_DIR/.env"
chown -R azp-agent:azp-agent "$HOME_DIR"

parameter() {
  "$VENV/bin/python" -c '
import sys, boto3
value = boto3.client("ssm").get_parameter(Name="/release-demo/" + sys.argv[1], WithDecryption=True)["Parameter"]["Value"]
print("" if value == "unset" else value)' "$1" 2>/dev/null || true
}

if [[ ! -f $AGENT_DIR/.agent ]]; then
  organization=$(parameter azure-devops/organization)
  VSTS_AGENT_INPUT_TOKEN=$(parameter azure-devops/agent-registration-token)
  if [[ -z $organization || -z $VSTS_AGENT_INPUT_TOKEN ]]; then
    echo "Azure Pipelines agent: no organization or registration token in SSM yet. Not registered."
  else
    # The token reaches config.sh in its environment, out of ps.
    export VSTS_AGENT_INPUT_TOKEN
    sudo -u azp-agent --preserve-env=VSTS_AGENT_INPUT_TOKEN "$AGENT_DIR/config.sh" --unattended \
      --url "https://dev.azure.com/$organization" --auth pat --pool release-demo --agent tools-host \
      --work "$HOME_DIR/_work" --replace --acceptTeeEula
    echo "Azure Pipelines agent: registered as tools-host"
  fi
  unset VSTS_AGENT_INPUT_TOKEN
fi

cat > /etc/systemd/system/release-demo-azure-agent.service <<UNIT
[Unit]
Description=Azure Pipelines agent tools-host (demo 22), started by scripts/demo.sh up azure-devops
After=docker.service network-online.target
Wants=network-online.target

[Service]
User=azp-agent
WorkingDirectory=$AGENT_DIR
ExecStart=$AGENT_DIR/run.sh
KillMode=process
KillSignal=SIGINT
TimeoutStopSec=5min
Restart=on-failure
RestartSec=10
UNIT
systemctl daemon-reload
