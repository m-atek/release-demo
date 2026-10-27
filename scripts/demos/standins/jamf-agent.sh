#!/bin/bash
# The pilot Mac's jamf command, for the Jamf Pro stand-in in jamf.py. It takes
# the real binary's place on a Mac enrolled in the stand-in, and prints what
# the real one prints, so sudo jamf policy looks and works the same:
#
#   jamf enroll -url <stand-in URL> -code <invitation>   once, by pilot_setup.sh
#   jamf policy        run the policies due at check-in: download, verify, install, report
#   jamf recon         send the inventory: every app in /Applications with its version
#   jamf version
#
# Installed as /usr/local/bin/jamf by scripts/demos/desktop/pilot_setup.sh,
# which also adds the LaunchDaemon that checks in every 15 minutes, as the
# real agent does. Runs as root: installer needs it.
#
# For the tests: JAMF_AGENT_ROOT puts the configuration and /Applications
# under another folder, and then packages are unpacked there instead of
# installed, so no root is needed. JAMF_AGENT_NAME and JAMF_AGENT_SERIAL
# stand in for the computer's name and serial number.

set -euo pipefail

ROOT=${JAMF_AGENT_ROOT:-}
CONF_DIR="$ROOT/Library/Application Support/JamfStandin"
CONF="$CONF_DIR/agent.conf"
APPS="$ROOT/Applications"
VERSION="11.22.0-standin"

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

need_root() {
  [[ -n $ROOT || $(id -u) -eq 0 ]] || { echo "This command requires root: run sudo jamf $1"; exit 1; }
}

load_conf() {
  [[ -r $CONF ]] || { echo "This Mac isn't enrolled. Run scripts/demos/desktop/pilot_setup.sh first."; exit 1; }
  # shellcheck disable=SC1090
  source "$CONF"
}

# post <path> <curl data arguments...>: a call to the stand-in as this computer.
post() {
  local path=$1
  shift
  curl -fsS --max-time 60 -X POST -H "Authorization: Bearer $TOKEN" \
    --data-urlencode "computer_id=$COMPUTER_ID" "$@" "$JSS_URL$path"
}

enroll() {
  need_root enroll
  local url="" code=""
  while [[ $# -gt 0 ]]; do
    case $1 in
      -url) url=${2%/}; shift 2 ;;
      -code) code=$2; shift 2 ;;
      *) usage ;;
    esac
  done
  [[ -n $url && -n $code ]] || usage
  local name serial udid reply
  name=$(computer_name)
  serial=$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformSerialNumber/ {print $4}')
  udid=$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/ {print $4}')
  echo "Enrolling with $url..."
  reply=$(curl -fsS --max-time 60 -X POST --data-urlencode "code=$code" --data-urlencode "name=$name" \
    --data-urlencode "serial=${JAMF_AGENT_SERIAL:-$serial}" --data-urlencode "udid=$udid" \
    --data-urlencode "os=$(sw_vers -productVersion)" "$url/standin/agent/enroll")
  mkdir -p "$CONF_DIR"
  chmod 700 "$CONF_DIR"
  umask 077
  {
    echo "JSS_URL='$url'"
    echo "COMPUTER_ID='$(sed -n 's/^computer_id=//p' <<<"$reply")'"
    echo "TOKEN='$(sed -n 's/^token=//p' <<<"$reply")'"
  } > "$CONF"
  echo "Enrolled $name as computer $(sed -n 's/^computer_id=//p' <<<"$reply")."
  recon
}

computer_name() {
  echo "${JAMF_AGENT_NAME:-$(scutil --get ComputerName 2>/dev/null || hostname -s)}"
}

app_field() {
  /usr/bin/plutil -extract "$2" raw -o - "$1/Contents/Info.plist" 2>/dev/null || true
}

recon() {
  need_root recon
  load_conf
  echo "Retrieving inventory preferences from $JSS_URL/..."
  echo "Locating accounts..."
  echo "Locating applications..."
  echo "Searching path: /Applications"
  local inventory="" app
  shopt -s nullglob
  for app in "$APPS"/*.app; do
    inventory+="$(basename "$app" .app)	$(app_field "$app" CFBundleShortVersionString)	$(app_field "$app" CFBundleIdentifier)	/Applications/$(basename "$app")"$'\n'
  done
  echo "Locating hard drive information..."
  echo "Submitting data to $JSS_URL/..."
  post /standin/agent/inventory --data-urlencode "applications=$inventory" --data-urlencode "os=$(sw_vers -productVersion)"
}

install_pkg() {
  local pkg=$1
  if [[ -n $ROOT ]]; then
    # Tests: unpack the payload under JAMF_AGENT_ROOT instead of installing.
    local expanded
    expanded=$(mktemp -d)
    pkgutil --expand-full "$pkg" "$expanded/x" >/dev/null
    mkdir -p "$APPS"
    find "$expanded/x" -path '*/Payload/Applications/*.app' -maxdepth 4 -prune | while read -r app; do
      rm -rf "$APPS/$(basename "$app")"
      cp -R "$app" "$APPS/"
    done
    rm -rf "$expanded"
    return
  fi
  # The stand-in's packages are signed with an identity Apple didn't issue.
  /usr/sbin/installer -pkg "$pkg" -target / -allowUntrusted >/dev/null
}

policy() {
  need_root policy
  load_conf
  local user due
  user=$(stat -f %Su /dev/console 2>/dev/null || echo root)
  echo "Checking for policies triggered by \"recurring check-in\" for user \"$user\"..."
  due=$(post /standin/agent/checkin --data-urlencode "name=$(computer_name)")
  if [[ -z ${due//[$'\n' ]/} ]]; then
    echo "No policies were found for the \"recurring check-in\" trigger."
    return
  fi
  WORK=$(mktemp -d)
  trap 'rm -rf "$WORK"' EXIT
  local recon_after=0 kind policy_id name file sha512 recon_flag url status
  while IFS=$'\t' read -r kind policy_id name file sha512 recon_flag url; do
    [[ $kind == policy ]] || continue
    echo "Executing Policy $name"
    echo "Downloading $file..."
    status=Completed
    if ! curl -fsS --max-time 600 -o "$WORK/$file" "$url"; then
      echo "Error: could not download $file."
      status=Failed
    else
      echo "Verifying package integrity..."
      if [[ $(shasum -a 512 "$WORK/$file" | cut -d' ' -f1) != "$sha512" ]]; then
        echo "Error: $file does not match its checksum."
        status=Failed
      else
        echo "Installing $file..."
        if install_pkg "$WORK/$file"; then
          echo "Successfully installed $file."
        else
          echo "Installation failed. The installer reported: installer: Package name is $file"
          status=Failed
        fi
      fi
    fi
    [[ $recon_flag == 1 ]] && recon_after=1
    echo "Submitting log to $JSS_URL/..."
    post /standin/agent/log --data-urlencode "policy_id=$policy_id" --data-urlencode "status=$status" \
      --data-urlencode "details=$file" >/dev/null
  done <<<"$due"
  if [[ $recon_after == 1 ]]; then
    echo "Running Recon..."
    recon
  fi
}

[[ $# -ge 1 ]] || usage
command=$1
shift
case $command in
  enroll) enroll "$@" ;;
  policy) policy ;;
  recon) recon ;;
  version) echo "version=$VERSION" ;;
  *) usage ;;
esac
