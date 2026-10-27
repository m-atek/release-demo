"""Give the main demo a past: release orders-service 1.2.0 to 1.3.0, the day before recording.

    python scripts/history.py              from the Mac: runs on the tools host, prints its progress
    python scripts/history.py --follow     show the run on the host again, after Ctrl-C
    python scripts/history.py --dry-run    the plan and what is done already; changes nothing

Eleven standard changes under ORD-1 to ORD-11, one at a time: stage.py puts
the version on main, then the release job starts as release-bot with the
parameters the Slack handler sends, as if Sam asked in #releases, and this
waits for it. 1.2.4 and 1.2.8 fail the production check on purpose and roll
back, each for its own reason (see demo-states/orders-service/). It takes
about 40 minutes and ends with production on 1.3.0, so the dashboard, Jira,
ServiceNow, Splunk and Slack have a real past. A failing gate or a new
critical finding shows here a day before a take.

It resumes: a version whose last run ended as planned is skipped, a run
still going is waited for. After an interruption, run it again. A run that
ends any other way stops history.py: fix the cause and run it again.

It runs on the tools host, where Jenkins answers on localhost. From the Mac
it starts itself there through SSM Run Command (scripts/host.py), and Ctrl-C
stops the printing, not the run. --here runs it where you are.
"""

import argparse
import os
import sys
import time

import host
import run_state
from check import wait_for_version
from common import fail, get_parameter, service, ssm_client
from release_job import ReleaseJob, version_of

SERVICE = "orders-service"
LIVE, ROLLED_BACK = "live", "rolled back"
PLAN = [
    ("1.2.0", "ORD-1", LIVE),
    ("1.2.1", "ORD-2", LIVE),
    ("1.2.2", "ORD-3", LIVE),
    ("1.2.3", "ORD-4", LIVE),
    ("1.2.4", "ORD-5", ROLLED_BACK),  # production reads a store list setting it doesn't have
    ("1.2.5", "ORD-6", LIVE),
    ("1.2.6", "ORD-7", LIVE),
    ("1.2.7", "ORD-8", LIVE),
    ("1.2.8", "ORD-9", ROLLED_BACK),  # a brand setting no environment has; staging only checks /health
    ("1.2.9", "ORD-10", LIVE),
    ("1.3.0", "ORD-11", LIVE),
]
BASELINE = PLAN[-1][0]
ROLLBACK_LINE = "Production check failed: rolled back to"
RUN_TIMEOUT = 45 * 60
STATUS = {"SUCCESS": "ok", "FAILED": "failed", "ABORTED": "aborted", "UNSTABLE": "unstable"}
POLL_SECONDS = 10


def ours(item, key):
    return item["parameters"].get("SERVICE") == SERVICE and item["parameters"].get("JIRA_KEY") == key


def last_run(job, key):
    """The newest release run of this service for this Jira key, or None."""
    return next((build for build in job.builds(limit=100) if ours(build, key)), None)


def as_planned(job, build, version, expect):
    """Did this finished run end the way the plan says: live, or rolled back
    after the production check, with the previous version back and checked?"""
    if build["building"] or version_of(build) != version:
        return False
    if expect == LIVE:
        return build["result"] == "SUCCESS"
    return build["result"] == "FAILURE" and ROLLBACK_LINE in job.console(build["number"])


def outcome(build):
    if build["result"] == "SUCCESS":
        return "live"
    return (build["result"] or "unknown").lower()


def watch(job, number, label):
    """Print each stage as it ends, until the run ends. Returns the finished build."""
    deadline = time.monotonic() + RUN_TIMEOUT
    printed = set()
    while True:
        build = job.build(number)
        for name, status, seconds in job.stages(number):
            if status in ("IN_PROGRESS", "NOT_EXECUTED", "PAUSED_PENDING_INPUT") or name in printed:
                continue
            printed.add(name)
            print(f"    {name:<17} {STATUS.get(status, status.lower()):<8} {run_state.duration(seconds)}")
        if not build["building"]:
            return build
        if time.monotonic() > deadline:
            fail(f"{label}: build #{number} still runs after {RUN_TIMEOUT // 60} minutes. Look at it in Jenkins.")
        time.sleep(POLL_SECONDS)


def release(job, version, key, requester):
    """Stage the version, start its release run as the Slack handler would, return the build number."""
    host.stage(SERVICE, version)
    queue_id = job.start({"SERVICE": SERVICE, "JIRA_KEY": key, "OPTION": "standard",
                          "REQUESTED_BY": requester, "SLACK_CHANNEL": "", "SLACK_TS": ""})
    number = job.started(queue_id, waiting=lambda why: print(f"  queued: {why}"))
    print(f"  started build #{number}")
    return number


def production_version():
    """What production's live alias answers on /health, through its function URL."""
    from deploy import lambda_client

    function = service(SERVICE)["environments"]["production"]["function"]
    url = lambda_client(SERVICE).get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"]
    _, seen = wait_for_version(url.rstrip("/"), BASELINE, 30)
    return seen


def run(job, requester, dry_run=False):
    started = time.monotonic()
    print(f"History: {SERVICE} {PLAN[0][0]} to {BASELINE}, {len(PLAN)} standard changes"
          f"{' (dry run: nothing changes)' if dry_run else ', about 40 minutes'}.")
    todo = False
    for index, (version, key, expect) in enumerate(PLAN, 1):
        label = f"[{index}/{len(PLAN)}] {SERVICE} {version} for {key}"
        planned = "fails its production check on purpose and rolls back" if expect == ROLLED_BACK else "goes live"
        print(f"{label}: {planned}")
        build = last_run(job, key)
        queued = next((item for item in job.queued() if ours(item, key)), None)
        if queued:
            print(f"  a run for {key} waits in the queue: {'it carries on' if dry_run else 'waiting for it'}")
            if dry_run:
                todo = True
                continue
            build = watch(job, job.started(queued["id"]), label)
        elif build and build["building"]:
            print(f"  build #{build['number']} is still going: {'it carries on' if dry_run else 'waiting for it'}")
            if dry_run:
                todo = True
                continue
            build = watch(job, build["number"], label)
        elif build and as_planned(job, build, version, expect):
            print(f"  done earlier: build #{build['number']}, {outcome(build)}")
            continue
        elif dry_run:
            last = f" (last run #{build['number']}: {version_of(build)} {outcome(build)})" if build else ""
            print(f"  to do: stage {version} on main, then start the release job{last}")
            todo = True
            continue
        else:
            run_started = time.monotonic()
            build = watch(job, release(job, version, key, requester), label)
            print(f"  {outcome(build)} after {run_state.duration(time.monotonic() - run_started)}")
        if not as_planned(job, build, version, expect):
            fail(f"{label}: build #{build['number']} ended {outcome(build)}, but the plan says it {planned}. "
                 f"Look at the run in Jenkins, fix the cause, and run history.py again: it carries on from here.")
        if expect == ROLLED_BACK:
            print("  rolled back, as planned")

    if dry_run:
        print(f"Main has {SERVICE} {host.main_version(service(SERVICE)['path'])}. "
              f"{'Nothing to do.' if not todo else 'history.py would release the versions marked to do.'}")
        return
    seen = production_version()
    if seen != BASELINE:
        fail(f"Every release ran as planned, but production answers {seen or 'nothing'}, not {BASELINE}.")
    print(f"Done in {run_state.duration(time.monotonic() - started)}. Production runs {SERVICE} {BASELINE}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan and what is done, change nothing")
    parser.add_argument("--follow", action="store_true", help="from the Mac: show the run on the tools host")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()

    if not args.here and not host.on_host():
        return host.hand_over("history", "history.py", ["--dry-run"] if args.dry_run else [],
                              follow_only=args.follow, interval=10)
    if args.follow:
        fail("--follow is for the Mac. Here, read /var/log/release-demo-history.log.")
    if os.environ.get("BUILD_NUMBER") and host.unit_running("history") and not args.dry_run:
        fail("history.py is running on the tools host already, started from the Mac. Follow it: history.py --follow")
    sys.stdout.reconfigure(line_buffering=True)
    requester = get_parameter(ssm_client(), "slack/member-id/sam")
    try:
        run(ReleaseJob.as_release_bot(), requester, args.dry_run)
    except KeyboardInterrupt:
        print("\nInterrupted. A release run that started carries on in Jenkins; run history.py again to carry on.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
