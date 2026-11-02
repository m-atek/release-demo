"""Video 24, once: its two Jira issues, and the seed job.

    python scripts/demos/patch_clock/setup.py              from the Mac it runs itself on the tools host
    python scripts/demos/patch_clock/setup.py --dry-run    what it would make, and nothing else

Makes the next two free Jira keys, unless they exist: ORD-[f] "Show how many orders are packed on the
order page", and ORD-[p] "Patch brotli to 1.2.1 (CVE-2026-111497)". Prints them for the script's
placeholders. Then starts the Jenkins job patch-clock-seed, as admin, and waits for it, unless
orders-service-candidates/1.12.0/seed/ already holds the package. Safe to run again.
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import candidates  # noqa: E402
import host  # noqa: E402
import seed  # noqa: E402
from common import fail, get_parameter, ssm_client  # noqa: E402

JOB = "patch-clock-seed"
FEATURE = "Show how many orders are packed on the order page"
SEED_MINUTES = 8
LABEL = "orders-service"


def summaries():
    """(ORD-[f]'s summary, ORD-[p]'s summary)."""
    return FEATURE, candidates.chosen().patch_summary


def find(jira, summary):
    """The issue with exactly this summary, or None. The words before the first parenthesis are enough to
    search on: a parenthesis means something to Jira's text search."""
    words = summary.split(" (")[0]
    from jira import PROJECT

    found = jira.search(f'project = {PROJECT} AND labels = {LABEL} AND summary ~ "\\"{words}\\"" ORDER BY key', fields="summary,status")
    return next((issue for issue in found if issue["fields"]["summary"] == summary), None)


def issue(jira, summary, kind, description, dry_run):
    found = find(jira, summary)
    if found:
        return found["key"], "there already"
    if dry_run:
        return "ORD-?", "would make it"
    return jira.create(kind, summary, description, labels=[LABEL]), "made now"


def issues(dry_run=False):
    """Makes what is missing, the feature first, and prints both keys."""
    from jira import Jira

    jira = Jira()
    feature, patch = summaries()
    chosen = candidates.chosen()
    made = [issue(jira, feature, "Story", "The order page shows how many of today's orders are packed.", dry_run),
            issue(jira, patch, "Task", f"Move {chosen.package} to {chosen.good}, which fixes {chosen.cve}.", dry_run)]
    for (key, how), summary, name in zip(made, (feature, patch), ("ORD-[f]", "ORD-[p]")):
        print(f"{key}: {summary} ({how}). It is {name} in the script.")
    return [key for key, _ in made]


def admin_client():
    """Jenkins as admin, on the tools host: only admin may start the seed. A password needs a crumb."""
    from jcr_setup import JENKINS, Client

    client = Client(JENKINS, "admin", get_parameter(ssm_client(), "jenkins/admin-password"))
    status, crumb = client.call("GET", "/crumbIssuer/api/json")
    if status != 200:
        fail(f"Jenkins didn't answer ({status}). Is the tools host up? scripts/tools.sh status")
    return client, {crumb["crumbRequestField"]: crumb["crumb"]}


def last_build(client):
    status, reply = client.call("GET", f"/job/{JOB}/api/json?tree=lastBuild[number,building,result],inQueue")
    if status == 404:
        fail(f"Jenkins has no job {JOB} yet: it comes with the merge and the tools host's next boot, or a configuration reload.")
    if status != 200:
        fail(f"Jenkins GET {JOB}: {status}")
    return reply


def run_seed(sleep=time.sleep, deadline_seconds=SEED_MINUTES * 60):
    """The seed job, started as admin and waited for. Stops the run that calls it if the job fails."""
    client, crumb = admin_client()
    before = (last_build(client).get("lastBuild") or {}).get("number", 0)
    status, _ = client.call("POST", f"/job/{JOB}/build?delay=0sec", headers=crumb)
    if status not in (200, 201, 302):
        fail(f"Jenkins didn't start {JOB} ({status}).")
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        build = last_build(client).get("lastBuild") or {}
        if build.get("number", 0) > before and not build.get("building"):
            if build.get("result") != "SUCCESS":
                fail(f"{JOB} #{build['number']} ended {build.get('result')}: its log says why.")
            print(f"{JOB} #{build['number']}: seeded")
            return build["number"]
        sleep(5)
    fail(f"{JOB} didn't finish within {SEED_MINUTES} minutes. Look at it in Jenkins.")


def seeded():
    return seed.stored_package(get_parameter(ssm_client(), "jcr/token")) is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="what it would make, and nothing else")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        shown = ["--dry-run"] if args.dry_run else []
        return host.hand_over("patch-clock-setup", "demos/patch_clock/setup.py", shown, title=" ".join(["patch_clock setup.py", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    issues(args.dry_run)
    if seeded():
        print(f"{seed.SEED}/ holds the package: no seed needed.")
    elif args.dry_run:
        print(f"Would start the Jenkins job {JOB} as admin and wait for it.")
    else:
        run_seed()
    return 0


if __name__ == "__main__":
    sys.exit(main())
