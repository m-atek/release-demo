#!/usr/bin/env bash
# Make this Mac the Jenkins agent "mac" for Apple builds (demos 6 and 7). It
# connects out to Jenkins over WebSocket through Cloudflare Access with its
# own service token; nothing connects in.
#
#   scripts/demos/desktop/mac_agent_setup.sh            set up, or update, and start it
#   scripts/demos/desktop/mac_agent_setup.sh --remove   stop it and remove its LaunchAgent
#
# Needs: a Java runtime 17 or later (brew install --cask temurin@21), Xcode
# 26 or later for xcodebuild (the Command Line Tools build the Mac app too,
# with swift build), and your AWS sign-in. The values it needs come from
# SSM: the Mac agent's Cloudflare Access service token
# (cloudflare/mac-agent-client-id and -secret) and the node's Jenkins secret
# (jenkins/agent-secret/mac, or typed here, hidden, from Jenkins, Nodes,
# mac). They go into your login keychain, never a file; the agent reads them
# at each start. A LaunchAgent starts it when you log in and restarts it when
# it stops. Builds work in /Users/Shared/release-demo-jenkins.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=eu-west-1
LABEL=nl.es.release-demo.jenkins-agent
SERVICE=release-demo-jenkins-agent
SUPPORT="$HOME/Library/Application Support/release-demo"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WORK=/Users/Shared/release-demo-jenkins
JENKINS_URL=${JENKINS_URL:-https://jenkins-demo.es.nl/}

if [[ ${1:-} == --remove ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "The Mac agent is stopped and won't start at login. Its keychain items stay: security delete-generic-password -s $SERVICE"
  exit
fi

/usr/libexec/java_home -v 17+ >/dev/null 2>&1 ||
  { echo "No Java 17 or later. Install one: brew install --cask temurin@21"; exit 1; }
if [[ $(xcode-select -p) == *CommandLineTools* ]]; then
  echo "Note: only the Command Line Tools are installed. The Mac app builds with swift build; install Xcode 26 or later for xcodebuild (and for demo 7)."
fi
aws sts get-caller-identity >/dev/null 2>&1 || { echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"; exit 1; }

# keep <account> <value>: into the login keychain, readable by /usr/bin/security without a prompt.
# The value goes through security's stdin, not its command line.
keep() {
  security delete-generic-password -s "$SERVICE" -a "$1" >/dev/null 2>&1 || true
  printf 'add-generic-password -s %s -a %s -T /usr/bin/security -w %s\n' "$SERVICE" "$1" "$2" | security -i >/dev/null
  security find-generic-password -s "$SERVICE" -a "$1" >/dev/null || { echo "Couldn't store $1 in the keychain"; exit 1; }
}

from_ssm() {
  aws ssm get-parameter --with-decryption --name "/release-demo/$1" --query Parameter.Value --output text 2>/dev/null || true
}

for name in cloudflare/mac-agent-client-id cloudflare/mac-agent-client-secret; do
  value=$(from_ssm "$name")
  [[ -n $value && $value != unset ]] || { echo "/release-demo/$name isn't in SSM yet: infra/ makes the Mac agent's service token."; exit 1; }
  keep "${name#cloudflare/mac-agent-}" "$value"
done
secret=$(from_ssm jenkins/agent-secret/mac)
if [[ -z $secret || $secret == unset ]]; then
  read -r -s -p "The mac node's secret (Jenkins, Manage Jenkins, Nodes, mac; hidden): " secret
  echo
fi
[[ $secret =~ ^[0-9a-f]{64}$ ]] || { echo "That isn't a Jenkins agent secret (64 hex characters)."; exit 1; }
keep jenkins-secret "$secret"
unset value secret
echo "Kept the service token and the node secret in your login keychain ($SERVICE)"

mkdir -p "$SUPPORT" "$WORK" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$SUPPORT/jenkins-agent.sh" <<EOF
#!/bin/bash
# Started by launchd ($LABEL): the Jenkins agent "mac", with its values from the keychain.
set -euo pipefail
value() { security find-generic-password -s $SERVICE -a "\$1" -w; }
id=\$(value client-id) secret=\$(value client-secret)
private=\$(mktemp -d)
trap 'rm -rf "\$private"' EXIT
curl -fsS -H "CF-Access-Client-Id: \$id" -H "CF-Access-Client-Secret: \$secret" -o "\$private/agent.jar" ${JENKINS_URL}jnlpJars/agent.jar
( umask 077; value jenkins-secret > "\$private/secret" )
# The secret file goes once the agent has read it.
( sleep 60; rm -f "\$private/secret" ) &
exec "\$(/usr/libexec/java_home -v 17+)/bin/java" -jar "\$private/agent.jar" -url $JENKINS_URL -name mac \\
  -secret "@\$private/secret" -webSocket -webSocketHeader "CF-Access-Client-Id=\$id" \\
  -webSocketHeader "CF-Access-Client-Secret=\$secret" -workDir $WORK
EOF
chmod 700 "$SUPPORT/jenkins-agent.sh"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$SUPPORT/jenkins-agent.sh</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>WorkingDirectory</key><string>$WORK</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/release-demo-jenkins-agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/release-demo-jenkins-agent.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "The Mac agent starts now and at every login. Its log: ~/Library/Logs/release-demo-jenkins-agent.log"
echo "Jenkins shows it under Nodes as mac once it connects. Keep the Mac awake while it builds."
