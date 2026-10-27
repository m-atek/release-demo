#!/usr/bin/env bash
# Does everything on the tools host answer? scripts/tools.sh runs this through
# SSM Run Command and waits for "Ready". Splunk and the tunnel count only once
# they are set up (terms accepted, tunnel token present).

SECRETS=/run/release-demo/secrets
ok=true

check() {
  local name=$1
  shift
  if "$@" >/dev/null 2>&1; then
    printf '  %-26s ok\n' "$name"
  else
    printf '  %-26s waiting\n' "$name"
    ok=false
  fi
}

if systemctl is-active --quiet release-demo-boot.service; then
  echo "  boot                       done"
else
  echo "  boot                       $(systemctl show -p ActiveState --value release-demo-boot.service)"
  ok=false
fi

admin="admin:$(cat $SECRETS/jenkins-admin-password 2>/dev/null)"
check "Jenkins" curl -sf http://localhost:8080/login -o /dev/null
check "Jenkins agent tools-host" bash -c "curl -sf -u '$admin' 'http://localhost:8080/computer/tools-host/api/json?tree=offline' | grep -q '\"offline\":false'"
check "JCR" bash -c "[[ \$(curl -sf http://localhost:8082/artifactory/api/system/ping) == OK ]]"
if [[ -f $SECRETS/splunk-general-terms-accepted ]]; then
  check "Splunk HEC" curl -sf http://localhost:8088/services/collector/health -o /dev/null
else
  echo "  Splunk                     not started: terms not accepted"
fi
if [[ -s $SECRETS/cloudflare-tunnel-token ]]; then
  check "Cloudflare tunnel" curl -sf http://127.0.0.1:2000/ready -o /dev/null
else
  echo "  Cloudflare tunnel          not started: no tunnel token yet"
fi

$ok && echo "Ready"
