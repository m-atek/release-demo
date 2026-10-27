#!/usr/bin/env bash
# Make a Mac the pilot Mac, from your own Mac, over SSH: a spare Mac, or a
# macOS virtual machine on your Mac (never your work Mac).
#
#   scripts/demos/desktop/pilot_setup.sh admin@192.168.64.5
#
# On the pilot Mac it sets the computer name to pilot-mac-01 (the Pilot
# group takes Macs named like "pilot"), the orders source Store Assistant
# reads (the preference nl.es.StoreAssistant OrdersURL, orders-service's
# production URL), installs the Jamf stand-in's agent as /usr/local/bin/jamf
# with a LaunchDaemon that checks in every 15 minutes, lets admins run
# sudo jamf policy without a password, and enrolls it. The enrollment
# invitation goes over SSH's stdin, never on a command line.
#
# The pilot Mac needs Remote Login (SSH) on and an admin account; sudo asks
# for its password once. With the Jamf Pro trial (STANDIN_JAMF=0) the pilot
# Mac enrolls with Jamf's own user-initiated enrollment instead, and only the
# name and the orders source are set here.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=eu-west-1
target=${1:?usage: pilot_setup.sh <user>@<pilot Mac>}
name=${PILOT_NAME:-pilot-mac-01}

parameter() {
  aws ssm get-parameter --with-decryption --name "/release-demo/$1" --query Parameter.Value --output text
}

orders_url=$(aws lambda get-function-url-config --function-name orders-service-production --qualifier live \
  --query FunctionUrl --output text)
standin=${STANDIN_JAMF:-${STANDIN:-1}}

echo "Copying the agent and the install steps to $target"
scp -q "$ROOT/scripts/demos/standins/jamf-agent.sh" "$target:/tmp/jamf-agent.sh"
# The stand-in's URL and invitation go through SSH's stdin into a file only
# that user can read; the install deletes it.
{
  if [[ $standin != 0 ]]; then
    parameter jamf/standin-url
    parameter jamf/standin-enrollment-code
  fi
} | ssh "$target" 'umask 077; cat > /tmp/jamf-enroll'
ssh "$target" 'cat > /tmp/pilot-install.sh' <<'INSTALL'
set -euo pipefail
name=$1 orders_url=$2
{ read -r url || true; read -r code || true; } < /tmp/jamf-enroll
rm -f /tmp/jamf-enroll
scutil --set ComputerName "$name"; scutil --set LocalHostName "$name"; scutil --set HostName "$name"
defaults write /Library/Preferences/nl.es.StoreAssistant OrdersURL "$orders_url"
chmod 644 /Library/Preferences/nl.es.StoreAssistant.plist
if [[ -n ${url:-} ]]; then
  install -d -m 755 /usr/local/bin
  install -m 755 /tmp/jamf-agent.sh /usr/local/bin/jamf
  cat > /Library/LaunchDaemons/nl.es.jamf-standin.checkin.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>nl.es.jamf-standin.checkin</string>
  <key>ProgramArguments</key><array><string>/usr/local/bin/jamf</string><string>policy</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>/var/log/jamf.log</string>
  <key>StandardErrorPath</key><string>/var/log/jamf.log</string>
</dict></plist>
PLIST
  launchctl bootout system/nl.es.jamf-standin.checkin 2>/dev/null || true
  /usr/local/bin/jamf enroll -url "$url" -code "$code"
  launchctl bootstrap system /Library/LaunchDaemons/nl.es.jamf-standin.checkin.plist
  # sudo jamf policy without a password prompt: on camera, and for reset.py --pilot-ssh.
  echo '%admin ALL=(root) NOPASSWD: /usr/local/bin/jamf policy' > /etc/sudoers.d/jamf-standin
  chmod 440 /etc/sudoers.d/jamf-standin
  visudo -cf /etc/sudoers.d/jamf-standin >/dev/null
fi
rm -f /tmp/jamf-agent.sh /tmp/pilot-install.sh
INSTALL

echo "Setting up $target as $name (sudo asks for its password)"
ssh -t "$target" sudo bash /tmp/pilot-install.sh "$name" "$orders_url"
echo "Done. On the pilot Mac, sudo jamf policy runs the policies now instead of at the next check-in."
