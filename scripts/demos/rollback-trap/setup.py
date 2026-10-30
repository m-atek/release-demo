"""Demo 20, once: the Jira issues, the database, and a real release of 1.6.0.

    python scripts/demos/rollback-trap/setup.py              from the Mac
    python scripts/demos/rollback-trap/setup.py --skip-neon  when neon.py setup has run already

1. Jira: ORD-[s] "Read today's orders from the database" and ORD-[t] "One
   name for the delivery estimate", the next free keys, made once and printed
   for the script's placeholders.
2. neon.py setup: the database, its branches and roles, and the connection
   strings in SSM.
3. On the tools host, where Jenkins answers: 1.6.0 staged on main and released
   for real, a standard release as Sam, the way history.py starts one, so both
   aliases point at a published 1.6.0 with the database settings.
4. Both Lambda version numbers of 1.6.0 into SSM, orders-service/baseline-versions,
   where reset.py rollback-trap finds them.

The main Terraform root must have set DATABASE_URL_PARAMETER on both
functions first (scripts/tf.sh apply): a published version keeps the
settings it was published with.
"""

import argparse
import json
import sys
import time

import trap  # first: it puts scripts/ on the path, where host and common live
import host  # noqa: E402
from common import get_parameter, put_parameter, ssm_client  # noqa: E402

ISSUES = {"s": ("Read today's orders from the database", "The page and /api/orders read the orders from Postgres."),
          "t": ("One name for the delivery estimate", "The orders table calls the delivery estimate "
                "delivery_estimate_days everywhere.")}
BASELINE = "1.6.0"


def issue_keys():
    """{s: key, t: key}, each issue made once, found again by its summary."""
    from jira import PROJECT, Jira

    jira = Jira()
    keys = {}
    for letter, (summary, description) in ISSUES.items():
        found = [i["key"] for i in jira.search(f'project = {PROJECT} AND summary ~ "\\"{summary}\\""', fields="summary")
                 if i["fields"]["summary"] == summary]
        keys[letter] = found[0] if found else jira.create("Story", summary, description, labels=["orders-service"])
        print(f"ORD-[{letter}] is {keys[letter]}: {summary}{'' if found else ' (made now)'}")
    return keys


def release(key):
    """On the tools host: stage 1.6.0 and release it as Sam, as history.py does, and wait for it."""
    from history import watch
    from release_job import ReleaseJob

    job = ReleaseJob.as_release_bot()
    host.stage(trap.SERVICE, BASELINE)
    queue_id = job.start({"SERVICE": trap.SERVICE, "JIRA_KEY": key, "OPTION": "standard",
                          "REQUESTED_BY": get_parameter(ssm_client(), "slack/member-id/sam"),
                          "SLACK_CHANNEL": "", "SLACK_TS": ""})
    number = job.started(queue_id, waiting=lambda why: print(f"  queued: {why}"))
    print(f"Release of {trap.SERVICE} {BASELINE} for {key}: build #{number}")
    build = watch(job, number, f"{trap.SERVICE} {BASELINE}")
    if build["result"] != "SUCCESS":
        print(f"Build #{number} ended {build['result']}: look at it in Jenkins, fix the cause and run setup.py again.")
        return 1
    print(f"{trap.SERVICE} {BASELINE} is live in staging and production.")
    return 0


def record_versions():
    """The aliases' versions, now on 1.6.0, for the reset. Your sign-in reads the functions directly."""
    from common import aws_session

    client = aws_session().client("lambda")
    versions = {}
    for name in ("staging", "production"):
        function = trap.environment(name)["function"]
        number = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
        description = client.get_function_configuration(FunctionName=function, Qualifier=number)["Description"]
        if description != BASELINE:
            raise SystemExit(f"{function}'s live alias is on version {number} ({description}), not {BASELINE}.")
        versions[name] = number
    put_parameter(ssm_client(), f"{trap.SERVICE}/baseline-versions", json.dumps(versions), secure=False)
    print(f"SSM: {trap.SERVICE}/baseline-versions = {json.dumps(versions)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-neon", action="store_true", help="neon.py setup has run already")
    parser.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release", metavar="KEY", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.release:  # the tools host's part
        sys.stdout.reconfigure(line_buffering=True)
        return release(args.release)
    started = time.monotonic()
    keys = issue_keys()
    if not args.skip_neon:
        import neon

        neon.setup()
    code = host.hand_over("setup-rollback-trap", "demos/rollback-trap/setup.py", ["--release", keys["s"]],
                          interval=10, title=f"setup.py rollback-trap release {BASELINE}")
    if code != 0:
        return code
    record_versions()
    print(f"Demo 20 is set up, in {time.monotonic() - started:.0f}s. ORD-[s] {keys['s']}, ORD-[t] {keys['t']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
