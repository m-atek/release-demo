"""Keep the release's one Slack message up to date.

    python scripts/notify.py stage Build running
    python scripts/notify.py stage Build ok --seconds 12.4
    python scripts/notify.py line "Approved by Alex in ServiceNow."
    python scripts/notify.py finish live | failed | rolled-back

The Slack handler posts the first message and passes its channel and ts; a
run started some other way (history.py, the Jenkins UI) posts its own. Every
call rewrites the same message: the stages with their times, links to the
build and the change, and the lines the video quotes.
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

import run_state
from common import get_parameter, ssm_client

ICON = {"waiting": "○", "running": ":hourglass_flowing_sand:", "ok": ":white_check_mark:",
        "failed": ":x:", "skipped": ":heavy_minus_sign:"}


def slack(method, token, **fields):
    request = urllib.request.Request(f"https://slack.com/api/{method}", data=json.dumps(fields).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=15) as response:
        reply = json.loads(response.read())
    if not reply.get("ok"):
        print(f"Slack {method}: {reply.get('error')}", file=sys.stderr)
    return reply


def requester(slack_id):
    """A Slack mention for a member id, which is all the Slack handler passes."""
    if re.fullmatch(r"[UW][A-Z0-9]{8,}", slack_id or ""):
        return f"<@{slack_id}>"
    return slack_id or "the Jenkins UI"


def render(state):
    head = (f"*Release {state['service']} {state['version']}* · {state['jira_key']} · "
            f"requested by {requester(state.get('requested_by'))}")
    rows = []
    for name, stage in state["stages"].items():
        took = f"  _{run_state.duration(stage['seconds'])}_" if stage.get("seconds") else ""
        rows.append(f"{ICON[stage['status']]}  {name}{took}")
    links = []
    if state["build"].get("url"):
        links.append(f"<{state['build']['url']}|Build #{state['build']['number']}>")
    if state.get("change", {}).get("url"):
        links.append(f"<{state['change']['url']}|{state['change']['number']}>")
    lines = [head, "\n".join(rows)]
    # A deploy script's own rows under the stages, for example demo 1's waves.
    if state.get("rows"):
        lines.append("\n".join(state["rows"]))
    if links:
        lines.append(" · ".join(links))
    lines += state.get("lines", [])
    return "\n\n".join(lines)


def post(state):
    token = get_parameter(ssm_client(), "slack/bot-token")
    text = render(state)
    if state["slack"].get("ts"):
        slack("chat.update", token, channel=state["slack"]["channel"], ts=state["slack"]["ts"], text=text)
    else:
        channel = state["slack"].get("channel") or get_parameter(ssm_client(), "slack/channel-id")
        reply = slack("chat.postMessage", token, channel=channel, text=text, unfurl_links=False)
        if reply.get("ok"):
            state["slack"] = {"channel": reply["channel"], "ts": reply["ts"]}
            run_state.save(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("name", help="a stage of this run, from .release/state.json")
    stage.add_argument("status", choices=list(ICON))
    stage.add_argument("--seconds", type=float)
    line = sub.add_parser("line")
    line.add_argument("text")
    finish = sub.add_parser("finish")
    finish.add_argument("result", choices=["live", "failed", "rolled-back", "cancelled"])
    finish.add_argument("--previous", default="")
    args = parser.parse_args()

    state = run_state.load()
    if args.command == "stage":
        if args.name not in state["stages"]:
            raise SystemExit(f"No stage {args.name} in this run: {', '.join(state['stages'])}")
        state["stages"][args.name]["status"] = args.status
        if args.seconds is not None:
            state["stages"][args.name]["seconds"] = args.seconds
    elif args.command == "line":
        state.setdefault("lines", []).append(args.text)
    else:
        total = run_state.duration(run_state.now() - state["started_at"])
        state["total"] = total
        text = {"live": f"Live in {total}.",
                # A demo's deploy script may set finish_line in the run state for how
                # its stop reads (for example a wave that stopped at one server).
                "failed": (state["finish_line"].format(total=total) if state.get("finish_line") else
                           f"Stopped after {total}, after the production deploy. Look at production now."
                           if "production" in state.get("deploys", {}) else f"Stopped after {total}. Nothing reached production."),
                "rolled-back": f"Production check failed. Rolled back to {args.previous}.",
                "cancelled": f"Cancelled after {total}."}[args.result]
        state.setdefault("lines", []).append(text)
    run_state.save(state)
    post(state)


if __name__ == "__main__":
    sys.exit(main())
