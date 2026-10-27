"""The rollback /rollback starts: its run state and its one Slack message.

    python scripts/run_state.py init --service orders-service --jira-key "" ... --requested-at 1793000000
    python scripts/rollback.py init                     make that state a rollback's, show it in Slack
    python scripts/rollback.py stage "Roll back" running
    python scripts/rollback.py stage "Roll back" ok --seconds 2.1
    python scripts/rollback.py moved                    after deploy --previous: which versions, into the state
    python scripts/rollback.py find-issue               the Jira issue of the release rolled back
    python scripts/rollback.py finish rolled-back | failed --reason "..."

The run state keeps the keys the shared scripts read (service, version,
jira_key, requested_by, build, change), so change.py, jira.py and events.py
work on a rollback as on a release. version is the version rolled back, as in
the release pipeline's own rollback event; rolled_back_to is the one now live.
The Slack message is the one the Slack handler posted, rewritten in place.
"""

import argparse
import sys
import urllib.parse

import notify
import run_state
from common import fail, get_parameter, service, ssm_client

STAGES = ["Roll back", "Check rollback", "Record rollback"]


def render(state):
    versions = f" {state['version']} to {state['rolled_back_to']}" if state.get("rolled_back_to") else ""
    head = (f"*Rollback {state['service']}{versions}* · production · "
            f"requested by {notify.requester(state.get('requested_by'))}")
    rows = []
    for name in STAGES:
        stage = state["stages"][name]
        took = f"  _{run_state.duration(stage['seconds'])}_" if stage.get("seconds") else ""
        rows.append(f"{notify.ICON[stage['status']]}  {name}{took}")
    links = []
    if state["build"].get("url"):
        links.append(f"<{state['build']['url']}|Build #{state['build']['number']}>")
    if state.get("change", {}).get("url"):
        links.append(f"<{state['change']['url']}|{state['change']['number']}>")
    if state.get("jira_key"):
        links.append(state["jira_key"])
    lines = [head, "\n".join(rows)]
    if links:
        lines.append(" · ".join(links))
    lines += state.get("lines", [])
    return "\n\n".join(lines)


def post(state):
    """Rewrite the handler's message; a run started from the Jenkins UI posts its own."""
    ssm = ssm_client()
    token = get_parameter(ssm, "slack/bot-token")
    text = render(state)
    if state["slack"].get("ts"):
        notify.slack("chat.update", token, channel=state["slack"]["channel"], ts=state["slack"]["ts"], text=text)
    else:
        channel = state["slack"].get("channel") or get_parameter(ssm, "slack/channel-id")
        reply = notify.slack("chat.postMessage", token, channel=channel, text=text, unfurl_links=False)
        if reply.get("ok"):
            state["slack"] = {"channel": reply["channel"], "ts": reply["ts"]}


def init(state):
    """A rollback rolls back what is live, not the head of main: no version or
    Jira issue until the move says which."""
    state.update(kind="rollback", version="", jira_key="", option="",
                 stages={name: {"status": "waiting"} for name in STAGES})
    return state


def moved(state):
    """deploy --previous records the version it left and the one now live, as scripts/deploy.py does."""
    production = state.get("deploys", {}).get("production", {})
    if not production.get("previous_version") or not production.get("rolled_back_to"):
        fail("The deploy script's --previous didn't record previous_version and rolled_back_to "
             "under deploys.production in the run state (docs/adding-a-demo.md).")
    state["version"] = production["previous_version"]
    state["rolled_back_to"] = production["rolled_back_to"]
    return state


def find_issue(state, jira):
    """The issue with the fix version "<service> <version>", which the release
    pipeline gave the release now rolled back. None when there is none, such as
    for the version Terraform deployed first."""
    version = jira.version(f"{state['service']} {state['version']}", create=False)
    if not version:
        return None
    project = service(state["service"])["jira"]["project"]
    jql = f"project = {project} AND fixVersion = {version['id']} ORDER BY updated DESC"
    found = jira.call("GET", "/rest/api/3/search/jql?" + urllib.parse.urlencode(
        {"jql": jql, "fields": "key", "maxResults": 1}))
    issues = (found or {}).get("issues", [])
    return issues[0]["key"] if issues else None


def finish(state, result, reason=""):
    total = run_state.duration(run_state.now() - state["started_at"])
    state["total"] = total
    was, back = state.get("version"), state.get("rolled_back_to")
    reason = reason.strip().rstrip(".")
    if result == "rolled-back":
        line = f"Rolled back from {was} to {back} in {total}."
    elif back:
        line = f"Rolled back from {was} to {back}, but {reason}."
    else:
        line = f"Stopped after {total}: {reason}. Production didn't change."
    state.setdefault("lines", []).append(line)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    stage = sub.add_parser("stage")
    stage.add_argument("name", choices=STAGES)
    stage.add_argument("status", choices=list(notify.ICON))
    stage.add_argument("--seconds", type=float)
    sub.add_parser("moved")
    sub.add_parser("find-issue")
    finisher = sub.add_parser("finish")
    finisher.add_argument("result", choices=["rolled-back", "failed"])
    finisher.add_argument("--reason", default="it stopped")
    args = parser.parse_args()

    state = run_state.load()
    if args.command == "init":
        init(state)
    elif args.command == "stage":
        state["stages"][args.name]["status"] = args.status
        if args.seconds is not None:
            state["stages"][args.name]["seconds"] = args.seconds
    elif args.command == "moved":
        moved(state)
        print(f"{state['service']}: production rolled back from {state['version']} to {state['rolled_back_to']}")
    elif args.command == "find-issue":
        from jira import Jira  # only here: the other commands needn't reach Jira
        try:
            state["jira_key"] = find_issue(state, Jira()) or ""
        except SystemExit:  # Jira down or refusing: the rollback is done, say so and go on
            state["jira_key"] = ""
        print(f"Jira issue of {state['service']} {state['version']}: {state['jira_key'] or 'none found'}")
        run_state.save(state)
        return
    else:
        finish(state, args.result, args.reason)
    run_state.save(state)
    post(state)
    run_state.save(state)


if __name__ == "__main__":
    sys.exit(main())
