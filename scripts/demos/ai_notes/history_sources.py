"""Save the sources of one of history.py's rolled-back releases, for
explain.py --from-files (video 13's acceptance check 8).

    python3 scripts/demos/ai_notes/history_sources.py 1.2.4 --out .local/history-1.2.4
    python3 scripts/demos/ai_notes/explain.py --from-files .local/history-1.2.4

On the tools host (Jenkins answers there) with the evidence reader role. It
reads the release build of that version from Jenkins, its Check production
lines from the build's log, the failed version's CloudWatch lines between the
build's start and end, and the diff and setting names from the demo states.
Lambda keeps the logs 14 days, so run it within 14 days of history.py's run.
It writes only the folder; explain.py --from-files writes nowhere either.
"""

import argparse
import re
import sys

import notes  # noqa: F401  (puts scripts/ on the path)
import explain
from evidence import between_states, reader_session
from history import PLAN, SERVICE
from release_job import ReleaseJob, version_of

CHECK_LINE = re.compile(r"(?:^|\s)((?:ok {3}|FAIL )\S.*|(?:Staging|Production) check (?:passed|failed))$")


def build_of(job, version, key):
    for build in job.builds(limit=200):
        if build["parameters"].get("JIRA_KEY") == key and version_of(build) == version and not build["building"]:
            return build
    raise SystemExit(f"No finished release build of {SERVICE} {version} for {key} in Jenkins.")


def check_lines(console):
    """The failed production check's own lines from a build's log. After the
    rollback the pipeline checks the previous version too, and that check
    passes: it is the rollback's, so the last failed production check wins,
    as check-production.txt holds it in a live run."""
    blocks, current = [], []
    for raw in console.splitlines():
        match = CHECK_LINE.search(raw.rstrip())
        if not match:
            continue
        current.append(match.group(1))
        if match.group(1).endswith(("check passed", "check failed")):
            if match.group(1).startswith("Production"):
                blocks.append(current)
            current = []
    failed = [block for block in blocks if block[-1] == "Production check failed"]
    return (failed or blocks or [[]])[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="a release history.py rolled back, such as 1.2.4")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    plan = {version: (key, expect) for version, key, expect in PLAN}
    if args.version not in plan:
        raise SystemExit(f"{args.version} isn't one of history.py's releases.")
    index = [version for version, _, _ in PLAN].index(args.version)
    previous = PLAN[index - 1][0] if index else "1.1.0"
    key = plan[args.version][0]

    job = ReleaseJob.as_release_bot()
    build = build_of(job, args.version, key)
    start, end = build["timestamp"] / 1000, (build["timestamp"] + build["duration"]) / 1000
    session = reader_session()
    function = notes.service(SERVICE)["environments"]["production"]["function"]
    numbers = []
    for page in session.client("lambda").get_paginator("list_versions_by_function").paginate(FunctionName=function):
        numbers += [v["Version"] for v in page["Versions"] if v.get("Description") == args.version]
    if not numbers:
        raise SystemExit(f"{function} has no Lambda version of {args.version}.")
    number = max(numbers, key=int)
    events = explain.log_events(session.client("logs"), f"/aws/lambda/{function}", number, start, end + 30, wait=0)
    states = between_states(SERVICE, previous, args.version)
    evidence = {"run": {"service": SERVICE, "version": args.version, "jira_key": key, "build": str(build["number"]),
                        "build_url": f"{job.base}/job/release/{build['number']}/"},
                "diff": states["diff"], "settings": states["settings"], "findings": states["findings"]}
    failed = {"function": function, "lambda_version": number, "rolled_back_to": previous}
    explain.save_sources(args.out, evidence, check_lines(job.console(build["number"])), events, failed)
    print(f"Saved {args.out}: build #{build['number']}, version {number}, {len(events)} CloudWatch events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
