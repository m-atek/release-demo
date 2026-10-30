"""The release train (demo 21): at its departures, the release of what is new on main.

    python scripts/demos/release-train/train.py --timer        the job's timer run: leaves only at a departure
    python scripts/demos/release-train/train.py --rehearsal    leaves now, as a rehearsal
    python scripts/demos/release-train/train.py --dry-run      what it would do, starting nothing

For each service with a train key in services.yaml (orders-service): the
version on main against production's live version. Nothing new: one Slack
message, "No train today: nothing new since <version>.", and no run. Then
the issues in Jira's fix version "<service> <version>": each one not Done
must be named by a flag in the version's flags.yaml that is off in
production's AWS AppConfig; otherwise the train doesn't leave: "Train
stopped: <key> isn't done and isn't behind a flag that's off." Then it posts
the release's status message, one row per issue under the stages, and
starts the release job as release-bot with OPTION train, the keys
comma-separated, REQUESTED_BY "the 14:00 release train" and REQUESTED_AT the
message's own time, so "Live in" counts from the message the video's clock
starts on.

A timer run leaves only at a departure (services.yaml, train.departures,
Europe/Amsterdam, on train.days, weekdays unless a recording day adds one)
and only while scripts/demo.sh up release-train has the train switched on
(SSM release-train/enabled); any other tick ends at once with exit code 3,
which the job shows as not built.
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(1, str(HERE.parents[1]))

import flags  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, get_parameter, service, services, ssm_client  # noqa: E402

NOT_DEPARTING = 3
NOTE = Path(os.environ.get("WORKSPACE", ROOT)) / ".release" / "train.txt"


def now_in_amsterdam():
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo("Europe/Amsterdam"))
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc)


LATE_MINUTES = 10  # a timer run that waited for an executor still takes its departure
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS = DAYS[:5]


def train_days(trains):
    """The days the trains leave, in week order: services.yaml's train.days, weekdays by default."""
    wanted = {day for config in trains.values() for day in (config["train"].get("days") or WEEKDAYS)}
    unknown = wanted - set(DAYS)
    if unknown:
        raise SystemExit(f"services.yaml's train.days has {', '.join(sorted(unknown))}: use {', '.join(DAYS)}.")
    return [day for day in DAYS if day in wanted]


def spoken_days(days):
    return "weekdays" if days == WEEKDAYS else ", ".join(days)


def departure(moment, departures, days=WEEKDAYS):
    """The departure this timer run is for, "14:00", or None: on the train's days only,
    from the departure's minute to LATE_MINUTES after it."""
    if DAYS[moment.weekday()] not in days:
        return None
    for planned in departures:
        hour, minute = (int(part) for part in planned.split(":"))
        start = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= moment < start + datetime.timedelta(minutes=LATE_MINUTES):
            return planned
    return None


def note(text):
    """What the Jenkins run says in its description."""
    NOTE.parent.mkdir(parents=True, exist_ok=True)
    NOTE.write_text(text + "\n")
    print(text, flush=True)


def slack_post(text):
    import notify

    ssm = ssm_client()
    reply = notify.slack("chat.postMessage", get_parameter(ssm, "slack/bot-token"),
                         channel=get_parameter(ssm, "slack/channel-id"), text=text, unfurl_links=False)
    return reply


def live_version(name, config):
    """Production's live version, with the deploy role on the tools host, or your sign-in for a dry run on the Mac."""
    import deploy
    from common import aws_session

    function = config["environments"]["production"]["function"]
    try:
        client = deploy.lambda_client(name)
    except Exception:
        client = aws_session().client("lambda")
    return deploy.app_version(client, function, deploy.alias_version(client, function))


def issues(name, version):
    """The issues in the fix version: [{key, summary, status, done}]."""
    from jira import PROJECT, Jira

    found = Jira().search(f'project = {PROJECT} AND fixVersion = "{name} {version}" ORDER BY key',
                          fields="summary,status")
    return [{"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"],
             "done": i["fields"]["status"].get("statusCategory", {}).get("key") == "done"} for i in found]


def rows_for(found, declared, production):
    """(rows, the first issue that stops the train or None). A flag missing in production is off."""
    rows = []
    for issue in found:
        if issue["done"]:
            rows.append(f"{issue['key']} {issue['summary']}: done")
            continue
        behind = [flag for flag, settings in declared.items() if (settings or {}).get("issue") == issue["key"]]
        off = [flag for flag in behind if not production.get(flag, False)]
        if not off:
            return rows, issue["key"]
        rows.append(f"{issue['key']} {issue['summary']}: {issue['status'].lower()}, behind {off[0]} (off)")
    return rows, None


def status_message(name, version, keys, label, rows, config):
    import notify

    state = {"service": name, "version": version, "jira_key": ",".join(keys), "jira_keys": keys,
             "requested_by": label, "build": {"number": "", "url": ""}, "rows": rows,
             "stages": {stage: {"status": "waiting"} for stage in run_state.stages_for(config)}}
    return notify.render(state)


def leave(name, config, label, dry_run):
    """One service's train. Returns a line for the run's description."""
    version = (config["dir"] / "VERSION").read_text().strip()
    live = live_version(name, config)
    if version == live:
        if not dry_run:
            slack_post(f"No train today: nothing new since {live}.")
        return f"No train today: nothing new since {live}."
    found = issues(name, version)
    if not found:
        return f"Train stopped: Jira's {name} {version} has no issues."
    rows, stopped_by = rows_for(found, flags.declared(config["dir"]), flags.production_values())
    if stopped_by:
        line = f"Train stopped: {stopped_by} isn't done and isn't behind a flag that's off."
        if not dry_run:
            slack_post(line)
        return line
    keys = [issue["key"] for issue in found]
    text = status_message(name, version, keys, label, rows, config)
    if dry_run:
        print(text)
        return f"Would release {name} {version} for {', '.join(keys)}."
    posted = slack_post(text)
    if not posted.get("ok"):
        raise SystemExit(f"Slack didn't take the train's message: {posted.get('error')}")
    from release_job import ReleaseJob

    job = ReleaseJob.as_release_bot()
    queue_id = job.start({"SERVICE": name, "JIRA_KEY": ",".join(keys), "OPTION": "train", "REQUESTED_BY": label,
                          "SLACK_CHANNEL": posted["channel"], "SLACK_TS": posted["ts"], "REQUESTED_AT": posted["ts"],
                          "ROWS": json.dumps(rows)})
    number = job.started(queue_id, timeout=600, waiting=lambda why: print(f"  queued: {why}"))
    return f"Released {name} {version} for {', '.join(keys)}: release #{number}."


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--timer", action="store_true", help="the job's timer: leave only at a departure")
    how.add_argument("--rehearsal", action="store_true", help="leave now, as a rehearsal")
    how.add_argument("--dry-run", action="store_true", help="say what would happen, start nothing")
    args = parser.parse_args()

    trains = {name: config for name, config in services().items() if config.get("train")}
    moment = now_in_amsterdam()
    if args.timer:
        departures = sorted({d for config in trains.values() for d in config["train"]["departures"]})
        days = train_days(trains)
        at = departure(moment, departures, days)
        if not at:
            note(f"No departure at {moment:%a %H:%M}: the train leaves at {', '.join(departures)} on "
                 f"{spoken_days(days)}.")
            return NOT_DEPARTING
        try:
            enabled = get_parameter(ssm_client(), "release-train/enabled")
        except SystemExit:  # infra/demos/21-release-train-flags isn't applied yet: the train is off
            enabled = "off"
        if enabled != "on":
            note("The train is switched off: scripts/demo.sh up release-train switches it on.")
            return NOT_DEPARTING
        label = f"the {at} release train"
    else:
        label = "a release train rehearsal"
    lines = [leave(name, service(name), label, args.dry_run) for name in trains]
    note(" ".join(lines) or "No service has a train in services.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
