"""What one release run knows, shared by its stage scripts.

    python scripts/run_state.py init --service orders-service --jira-key ORD-12 ...
    python scripts/run_state.py get change.number
    python scripts/run_state.py set approval=standard

Kept in .release/state.json in the Jenkins workspace (or the repository when
run by hand), so every stage script reads the same version, commit, change,
Slack message and timings.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from common import ROOT, service, version_of

STAGES = ["Build", "Test", "Quality gate", "SBOM and scan", "Publish", "Staging", "Check staging",
          "Change", "Approval", "Production", "Check production", "Record"]
PATH = Path(os.environ.get("WORKSPACE", ROOT)) / ".release" / "state.json"
REPORTS = PATH.parent / "reports"


def load():
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def save(state):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(state, indent=2) + "\n")


def lookup(state, dotted):
    for part in dotted.split("."):
        state = state.get(part) if isinstance(state, dict) else None
    return state


def assign(state, dotted, value):
    *parents, last = dotted.split(".")
    for part in parents:
        state = state.setdefault(part, {})
    state[last] = value


def now():
    return time.time()


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration(seconds):
    """Minutes and seconds, never rounded down: 6m 42s."""
    seconds = int(-(-seconds // 1))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def requested_at(value):
    """The Slack command's time (epoch seconds), if given and plausible, else now."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return now()
    return seconds if 0 <= now() - seconds < 3600 else now()


def init(args):
    config = service(args.service)
    commit = os.environ.get("GIT_COMMIT") or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    commit_time = subprocess.run(["git", "log", "-1", "--format=%ct", commit], cwd=ROOT,
                                 capture_output=True, text=True).stdout.strip()
    state = {
        "service": args.service,
        "version": version_of(config["dir"]),
        "commit": commit,
        "commit_time": int(commit_time) if commit_time.isdigit() else None,
        "jira_key": args.jira_key,
        "option": args.option or "",
        "requested_by": args.requested_by or "",
        "slack": {"channel": args.slack_channel or "", "ts": args.slack_ts or ""},
        "build": {"number": os.environ.get("BUILD_NUMBER", "local"), "url": os.environ.get("BUILD_URL", "")},
        # From the moment Sam pressed Enter in Slack when the handler passes it,
        # so "Live in" matches the clock that starts at the command.
        "started_at": requested_at(args.requested_at),
        "stages": {name: {"status": "waiting"} for name in STAGES},
    }
    save(state)
    REPORTS.mkdir(parents=True, exist_ok=True)
    print(f"Release {args.service} {state['version']} for {args.jira_key}, commit {commit[:7]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("init")
    start.add_argument("--service", required=True)
    start.add_argument("--jira-key", required=True)
    start.add_argument("--option", default="")
    start.add_argument("--requested-by", default="")
    start.add_argument("--requested-at", default="", help="epoch seconds of the Slack command")
    start.add_argument("--slack-channel", default="")
    start.add_argument("--slack-ts", default="")
    getter = sub.add_parser("get")
    getter.add_argument("key")
    setter = sub.add_parser("set")
    setter.add_argument("pairs", nargs="+", help="key=value, dotted keys allowed")
    args = parser.parse_args()

    if args.command == "init":
        init(args)
    elif args.command == "get":
        value = lookup(load(), args.key)
        print("" if value is None else value)
    else:
        state = load()
        for pair in args.pairs:
            key, value = pair.split("=", 1)
            assign(state, key, value)
        save(state)


if __name__ == "__main__":
    sys.exit(main())
