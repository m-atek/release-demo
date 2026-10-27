#!/usr/bin/env bash
# Start, stop and check the tools host: Jenkins, JFrog Container Registry and
# Splunk Free.
#
#   scripts/tools.sh up       start the EC2 tools host, wait until everything answers: "Ready"
#   scripts/tools.sh status   its state, what answers, and any other demo server still running
#   scripts/tools.sh down     stop it; it also stops itself after 30 idle minutes
#
# Add --local to run the same stack in Docker on this Mac instead. Local
# secrets live in ~/.local/state/release-demo/secrets, outside the repository.

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

# The EC2 tools host. The AWS CLI starts and stops it; SSM Run Command asks it
# tools-host/ready.sh, so nothing needs an open port or another token.

aws_signed_in() {
  export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=eu-west-1
  aws sts get-caller-identity >/dev/null 2>&1 ||
    { echo "Not signed in to AWS. Run: aws sso login --profile $AWS_PROFILE"; exit 1; }
}

instance_id() {
  aws ec2 describe-instances --output text --query 'Reservations[].Instances[].InstanceId' \
    --filters Name=tag:Name,Values=release-demo-tools-host Name=instance-state-name,Values=pending,running,stopping,stopped
}

instance_state() {
  aws ec2 describe-instances --instance-ids "$1" --query 'Reservations[0].Instances[0].State.Name' --output text
}

# Runs ready.sh on the host and prints its answer; fails while SSM can't reach it.
host_ready() {
  local command
  command=$(aws ssm send-command --instance-ids "$1" --document-name AWS-RunShellScript \
    --parameters 'commands=["/opt/release-demo/tools-host/ready.sh"]' --query Command.CommandId --output text 2>/dev/null) || return 1
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    aws ssm get-command-invocation --command-id "$command" --instance-id "$1" \
      --query StandardOutputContent --output text 2>/dev/null && return
  done
  return 1
}

# Splunk starts only once Mateusz has accepted its General Terms, recorded in SSM.
splunk_terms_in_ssm() {
  local accepted
  accepted=$(aws ssm get-parameter --name /release-demo/splunk/general-terms-accepted --query Parameter.Value --output text)
  [[ $accepted != unset ]] && return
  [[ -t 0 ]] || { echo "Splunk's General Terms aren't accepted yet: Splunk won't start. Run up in a terminal to accept them."; return; }
  echo "Splunk starts only once you accept Splunk's General Terms:"
  echo "  $SPLUNK_TERMS_URL"
  local answer
  read -r -p "Do you accept them? [y/N] " answer
  [[ $answer =~ ^[Yy] ]] || return
  aws ssm put-parameter --overwrite --type String --name /release-demo/splunk/general-terms-accepted \
    --value "$(date -u +%Y-%m-%dT%H:%M:%SZ) accepted by $(aws sts get-caller-identity --query Arn --output text | sed 's#.*/##')" >/dev/null
  echo "Recorded. The host starts Splunk at its next boot."
}

aws_up() {
  aws_signed_in
  local id
  id=$(instance_id)
  [[ -n $id ]] || { echo "No tools host. Apply infra/ first: scripts/tf.sh apply"; exit 1; }
  splunk_terms_in_ssm

  case $(instance_state "$id") in
    stopped) aws ec2 start-instances --instance-ids "$id" >/dev/null; echo "Starting $id" ;;
    stopping) aws ec2 wait instance-stopped --instance-ids "$id"; aws ec2 start-instances --instance-ids "$id" >/dev/null ;;
  esac
  aws ec2 wait instance-running --instance-ids "$id"

  local start=$SECONDS answer=""
  echo "Waiting until everything answers (the first start builds images: about 15 minutes)"
  while (( SECONDS - start < 1800 )); do
    answer=$(host_ready "$id" || true)
    if grep -q '^Ready$' <<<"$answer"; then
      echo "$answer"
      echo "  Jenkins        https://jenkins-demo.es.nl"
      echo "  JCR            https://artifactory-demo.es.nl"
      echo "  Splunk         https://splunk-demo.es.nl"
      return
    fi
    sleep 15
  done
  echo "Not ready after 30 minutes. Last answer:"
  if [[ -n $answer ]]; then echo "$answer"; else echo "  the host does not answer SSM yet"; fi
  exit 1
}

aws_status() {
  aws_signed_in
  local id state
  id=$(instance_id)
  [[ -n $id ]] || { echo "No tools host. Apply infra/ first: scripts/tf.sh apply"; exit 1; }
  state=$(instance_state "$id")
  echo "Tools host $id: $state"
  [[ $state == running ]] && { host_ready "$id" || echo "  (doesn't answer SSM yet)"; }
  local others query
  query="Reservations[].Instances[?InstanceId!='$id'].[InstanceId,Tags[?Key=='Name']|[0].Value]"
  others=$(aws ec2 describe-instances --output text --query "$query" \
    --filters Name=tag:Project,Values=release-demo Name=instance-state-name,Values=pending,running)
  if [[ -n $others ]]; then
    echo "Other demo servers still running:"
    echo "$others" | sed 's/^/  /'
  else
    echo "No other demo servers running."
  fi
}

aws_down() {
  aws_signed_in
  local id
  id=$(instance_id)
  [[ -n $id ]] || { echo "No tools host."; exit 1; }
  aws ec2 stop-instances --instance-ids "$id" >/dev/null
  echo "Stopping $id. The data stays on its volume."
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

case $command in
  up) if $local_mode; then up; else aws_up; fi ;;
  down) if $local_mode; then down; else aws_down; fi ;;
  status) if $local_mode; then status; else aws_status; fi ;;
  *) usage ;;
esac
