"""What one release run knows, shared by its stage scripts.

    python scripts/run_state.py init --service orders-service --jira-key ORD-12 ...
    python scripts/run_state.py get change.number
    python scripts/run_state.py set approval=standard
    python scripts/run_state.py has "Migration check"     exit 0 when this run has the stage

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

import ci_env
from common import ROOT, service, version_of

STAGES = ["Build", "Test", "Quality gate", "SBOM and scan", "Publish", "Staging", "Check staging",
          "Change", "Approval", "Production", "Check production", "Record"]
PATH = Path(os.environ.get("WORKSPACE", ROOT)) / ".release" / "state.json"
REPORTS = PATH.parent / "reports"
# Stages only some services have, from services.yaml: (script key, stage, after which stage).
OPTIONAL_STAGES = [("contract", "Contract check", "SBOM and scan"), ("plan", "Plan", "Check staging")]
# Stages only some versions have (demo 20): (script key, stage, after the last of these stages
# the run has, folder the staged version must have). Only versions with migrations see them.
VERSION_STAGES = [("migration_check", "Migration check", ("SBOM and scan", "Contract check"), "migrations"),
                  ("rollback_check", "Rollback check", ("Check staging",), "migrations")]


def stages_for(config):
    """The stages of one service: the shared 12, plus its optional ones in their place."""
    stages = list(STAGES)
    for key, name, after in OPTIONAL_STAGES:
        if config.get("scripts", {}).get(key):
            stages.insert(stages.index(after) + 1, name)
    for key, name, after, folder in VERSION_STAGES:
        if config.get("scripts", {}).get(key) and config.get("dir") and (Path(config["dir"]) / folder).is_dir():
            stages.insert(max(stages.index(stage) for stage in after if stage in stages) + 1, name)
    return stages


def jira_keys(value):
    """The Jira keys of a run: one, or several comma-separated (demo 21's release train)."""
    return [key.strip() for key in (value or "").split(",") if key.strip()]


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
    run = ci_env.current()
    commit = run.commit or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    commit_time = subprocess.run(["git", "log", "-1", "--format=%ct", commit], cwd=ROOT,
                                 capture_output=True, text=True).stdout.strip()
    commit_time = int(commit_time) if commit_time.isdigit() else None
    # A CI that knows who asked and when (GitHub Actions: who merged, and the
    # merge commit's time) fills in what the Slack handler passes elsewhere.
    asked_at = args.requested_at or (commit_time if run.request_time_is_commit else None)
    state = {
        "service": args.service,
        "version": version_of(config["dir"]),
        "commit": commit,
        "commit_time": commit_time,
        "jira_key": args.jira_key,
        "jira_keys": jira_keys(args.jira_key),
        "option": args.option or "",
        "requested_by": args.requested_by or ci_env.requester(run) or "",
        "slack": {"channel": args.slack_channel or "", "ts": args.slack_ts or ""},
        "build": {"number": run.build_id, "url": run.run_url},
        # What the change and the events need to know of the CI that runs this.
        "ci": {"name": run.name, "correlation_id": run.correlation_id, "splunk": run.splunk, "origin": run.origin},
        # From the moment Sam pressed Enter in Slack when the handler passes it,
        # so "Live in" matches the clock that starts at the command.
        "started_at": requested_at(asked_at),
        "stages": {name: {"status": "waiting"} for name in stages_for(config)},
    }
    # Lines under the stages from whoever started the run (demo 21's release
    # train: one row per issue), shown in Slack and added to the change.
    rows = json.loads(args.rows) if args.rows else []
    if rows:
        state["rows"] = list(rows)
        state["change_details"] = ["Issues in this release:", *rows]
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
    start.add_argument("--rows", default="", help="lines under the stages, as a JSON list (the release train)")
    getter = sub.add_parser("get")
    getter.add_argument("key")
    setter = sub.add_parser("set")
    setter.add_argument("pairs", nargs="+", help="key=value, dotted keys allowed")
    has = sub.add_parser("has")
    has.add_argument("stage")
    args = parser.parse_args()

    if args.command == "init":
        init(args)
    elif args.command == "has":
        return 0 if args.stage in load().get("stages", {}) else 1
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
