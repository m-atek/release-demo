#!/usr/bin/env bash
# Start, stop and check the tools host: Jenkins, JFrog Container Registry and
# Splunk Free.
#
#   scripts/tools.sh up --local       start the stack on this Mac, wait until it answers
#   scripts/tools.sh status --local   what runs and what answers
#   scripts/tools.sh down --local     stop it; the data stays in Docker volumes
#
# Without --local these will drive the EC2 tools host, once infra/ exists.
# Local secrets live in ~/.local/state/release-demo/secrets, outside the repository.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE_DIR=${RELEASE_DEMO_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/release-demo}
SECRETS_DIR=$STATE_DIR/secrets
SPLUNK_TERMS_URL=https://www.splunk.com/en_us/legal/splunk-general-terms.html
SPLUNK_ACCEPTED=$STATE_DIR/splunk-general-terms-accepted
JENKINS=http://localhost:8080
JCR=http://localhost:8082
SPLUNK=http://localhost:8000
HEC=http://localhost:8088

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

compose() {
  local splunk_env=()
  if [[ -f $SPLUNK_ACCEPTED ]]; then
    splunk_env=(SPLUNK_START_ARGS=--accept-license SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com)
  fi
  env SECRETS_DIR="$SECRETS_DIR" JENKINS_URL="$JENKINS/" DOCKER_SOCKET_GID="${DOCKER_SOCKET_GID:-0}" \
    ${splunk_env[@]+"${splunk_env[@]}"} \
    docker compose -f "$ROOT/tools-host/docker-compose.yml" -f "$ROOT/tools-host/docker-compose.local.yml" "$@"
}

# Local-only secrets: random values made once and kept. Values from outside
# (the JCR token, later Slack, Jira and the rest) are written by their setup scripts.
ensure_secrets() {
  mkdir -p "$SECRETS_DIR"
  chmod 700 "$STATE_DIR" "$SECRETS_DIR"
  local name
  for name in jenkins-admin-password jenkins-release-bot-password jcr-db-password; do
    [[ -s $SECRETS_DIR/$name ]] || openssl rand -hex 16 > "$SECRETS_DIR/$name"
  done
  [[ -s $SECRETS_DIR/jcr.env ]] || printf 'JF_SHARED_DATABASE_PASSWORD=%s\n' "$(cat "$SECRETS_DIR/jcr-db-password")" > "$SECRETS_DIR/jcr.env"
  [[ -s $SECRETS_DIR/splunk-hec-token ]] || uuidgen | tr 'A-Z' 'a-z' > "$SECRETS_DIR/splunk-hec-token"
  if [[ ! -s $SECRETS_DIR/splunk.env ]]; then
    printf 'SPLUNK_PASSWORD=%s\nSPLUNK_HEC_TOKEN=%s\n' "$(openssl rand -hex 16)" "$(cat "$SECRETS_DIR/splunk-hec-token")" \
      > "$SECRETS_DIR/splunk.env"
  fi
  touch "$SECRETS_DIR/agent-secret"
  chmod 600 "$SECRETS_DIR"/*
}

# Splunk starts only after Mateusz accepts its General Terms here, once.
splunk_terms_accepted() {
  [[ -f $SPLUNK_ACCEPTED ]] && return 0
  [[ -t 0 ]] || return 1
  echo "Splunk starts only once you accept Splunk's General Terms:"
  echo "  $SPLUNK_TERMS_URL"
  local answer
  read -r -p "Do you accept them? [y/N] " answer
  if [[ $answer =~ ^[Yy] ]]; then
    date -u +%Y-%m-%dT%H:%M:%SZ > "$SPLUNK_ACCEPTED"
    return 0
  fi
  return 1
}

# wait_for <name> <seconds> <command...>: retry the command until it succeeds.
wait_for() {
  local name=$1 timeout=$2 start=$SECONDS
  shift 2
  printf '  %-34s' "$name"
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS - start > timeout )); then
      echo "no answer after ${timeout}s"
      return 1
    fi
    sleep 3
  done
  echo "ok ($((SECONDS - start))s)"
}

admin() { curl -sf -u "admin:$(cat "$SECRETS_DIR/jenkins-admin-password")" "$@"; }
jenkins_up() { curl -sf "$JENKINS/login" -o /dev/null; }
jcr_up() { [[ $(curl -sf "$JCR/artifactory/api/system/ping") == OK ]]; }
# Splunk answers early, then restarts while it provisions itself. Healthy means done.
splunk_up() {
  [[ $(docker inspect -f '{{.State.Health.Status}}' release-demo-splunk-1 2>/dev/null) == healthy ]] &&
    curl -sf "$SPLUNK/en-US/account/login" -o /dev/null && curl -sf "$HEC/services/collector/health" -o /dev/null
}
agent_online() { admin "$JENKINS/computer/tools-host/api/json?tree=offline" | grep -q '"offline":false'; }

fetch_agent_secret() {
  admin "$JENKINS/computer/tools-host/jenkins-agent.jnlp" | grep -o '[0-9a-f]\{64\}' | head -1 > "$SECRETS_DIR/agent-secret"
  [[ -s $SECRETS_DIR/agent-secret ]] || { echo "Couldn't read the agent secret from Jenkins"; exit 1; }
}

docker_socket_gid() {
  docker run --rm --entrypoint stat -v /var/run/docker.sock:/var/run/docker.sock release-demo/agent:local \
    -c %g /var/run/docker.sock
}

up() {
  docker info >/dev/null 2>&1 || { echo "Docker isn't running. Start Docker Desktop first."; exit 1; }
  ensure_secrets

  local services=(jenkins jcr-db jcr) with_splunk=true
  if splunk_terms_accepted; then
    services+=(splunk)
  else
    with_splunk=false
    echo "Starting without Splunk: its General Terms aren't accepted. Run up again in a terminal to accept them."
  fi

  echo "Building and starting ${services[*]}"
  compose up -d --build --quiet-pull "${services[@]}"
  wait_for "Jenkins $JENKINS" 300 jenkins_up

  fetch_agent_secret
  compose --profile agent build --quiet agent
  DOCKER_SOCKET_GID=$(docker_socket_gid) compose --profile agent up -d agent
  wait_for "Jenkins agent tools-host" 120 agent_online
  wait_for "JCR $JCR" 300 jcr_up
  if $with_splunk; then
    wait_for "Splunk $SPLUNK" 600 splunk_up
  fi

  echo "Ready"
  echo "  Jenkins admin password: $SECRETS_DIR/jenkins-admin-password"
}

status() {
  compose --profile agent ps --format 'table {{.Service}}\t{{.Status}}'
  echo
  check "Jenkins $JENKINS" jenkins_up
  check "Jenkins agent tools-host" agent_online
  check "JCR $JCR" jcr_up
  check "Splunk $SPLUNK" splunk_up
}

check() {
  printf '  %-34s' "$1"
  if "$2" >/dev/null 2>&1; then echo "answers"; else echo "no answer"; fi
}

down() {
  compose --profile agent --profile tunnel down
  echo "Stopped. The data stays in the Docker volumes."
}

[[ $# -ge 1 ]] || usage
command=$1
shift
local_mode=false
for arg in "$@"; do
  case $arg in
    --local) local_mode=true ;;
    *) usage ;;
  esac
done
if ! $local_mode; then
  echo "The EC2 tools host isn't built yet (infra/). Run the stack on this Mac with --local."
  exit 1
fi

case $command in
  up) up ;;
  down) down ;;
  status) status ;;
  *) usage ;;
esac
