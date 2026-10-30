# What releaseStage.groovy does under Jenkins, for the jobs of
# .github/workflows/release.yml, whose steps source this file (BASH_ENV).
#
#   stage Build -- python3 scripts/build.py orders-service --jira-key "$JIRA_KEY"
#   stage Staging --environment staging -- python3 scripts/deploy.py orders-service staging
#   skip Approval "A standard change is pre-approved."
#
# stage marks Slack's row running, runs the command, then gives the row its
# result and seconds, sends the event and, once the change exists, adds the
# work note, and returns the command's result. STAGE_STARTED (epoch seconds)
# counts a stage from an earlier moment: the approval, which waits between
# two jobs. Commands of more than one line are shell functions that return 1
# on their first failure: inside stage, bash ignores set -e.

state() {  # state <key>: one value of the run state, as run_state.py gets it
  python3 scripts/run_state.py get "$1"
}

stage() {  # stage <name> [--environment <environment>] [--deploy] -- <command...>
  local name=$1 environment="" deploy=""
  shift
  while [[ $# -gt 0 && $1 != -- ]]; do
    case $1 in
      --environment) environment=$2; shift 2 ;;
      --deploy) deploy=1; shift ;;
      *) echo "stage: unknown option $1" >&2; return 2 ;;
    esac
  done
  shift
  local started=${STAGE_STARTED:-$(date +%s.%N)} result=ok seconds
  python3 scripts/notify.py stage "$name" running
  "$@" || result=failed
  seconds=$(python3 -c 'import sys, time; print(f"{time.time() - float(sys.argv[1]):.1f}")' "$started")
  local extra=()
  [[ -n $environment ]] && extra+=(--environment "$environment")
  [[ -n $deploy && $result == ok ]] && extra+=(--deploy)
  python3 scripts/notify.py stage "$name" "$result" --seconds "$seconds"
  python3 scripts/events.py --stage "$name" --result "$result" --seconds "$seconds" ${extra[@]+"${extra[@]}"}
  python3 scripts/change.py note "$name: $result in ${seconds}s"
  [[ $result == ok ]]
}

skip() {  # skip <name> <reason>: releaseStage.skip
  python3 scripts/notify.py stage "$1" skipped
  python3 scripts/events.py --stage "$1" --result skipped
  python3 scripts/change.py note "$1: skipped. $2"
}

stop_reason() {  # stop_reason <text>: what Stopped and the change's notes say, as error() does in Jenkins
  python3 scripts/run_state.py set "stop_reason=$1"
}

ssm_secret() {  # ssm_secret <name> <variable>: one SSM value into a variable, masked before anything prints it
  local value
  value=$(aws ssm get-parameter --with-decryption --name "/release-demo/$1" --query Parameter.Value --output text) || return 1
  echo "::add-mask::$value"
  printf -v "$2" '%s' "$value"
}

# .release/ and dist/ go from job to job as one workflow artifact. A public
# repository's artifacts are public, and the run state names the ServiceNow
# instance, so the artifact is encrypted with a key only the jobs' AWS roles read.
save_state() {
  local RUN_STATE_KEY
  ssm_secret github/run-state-key RUN_STATE_KEY || return 1
  export RUN_STATE_KEY
  mkdir -p .release dist
  tar -czf - .release dist | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:RUN_STATE_KEY -out run-state.enc
}

restore_state() {
  local RUN_STATE_KEY
  ssm_secret github/run-state-key RUN_STATE_KEY || return 1
  export RUN_STATE_KEY
  openssl enc -d -aes-256-cbc -pbkdf2 -pass env:RUN_STATE_KEY -in run-state.enc | tar -xzf -
  rm -f run-state.enc
  {
    echo "VERSION=$(state version)"
    echo "JIRA_KEY=$(state jira_key)"
  } >> "$GITHUB_ENV"
}
