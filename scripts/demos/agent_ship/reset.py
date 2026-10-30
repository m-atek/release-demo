"""Put video 16 back where a take starts: production and main on 1.4.0 from a real release.

    python scripts/reset.py agent-ship              from the Mac: rulesets on here, the rest on the tools host
    python scripts/reset.py agent-ship --dry-run    every action it would take, and nothing else

On the Mac, with your gh login: video 16's rulesets on (ruleset.py --on).
Then on the tools host:

1-5. The main reset's first steps: release and rollback runs stopped, open
     orders-service changes cancelled, the take's tags and release copies
     archived, the Jira versions after 1.3.0 un-released (the fix version
     of the issue too) and ORD-12 and ORD-13 reopened.
6.   orders-service 1.4.0 in production and on main, from a real release:
     1.4.0 staged, then /release orders-service ORD-12 standard as Sam,
     as history.py starts one, with its own message in #releases. Skipped
     when production already runs 1.4.0 and main has it.
7.   Open pull requests by the coding agent closed with the comment
     "Earlier take". Merged ones stay in the history, and so do branches:
     each run makes a new one.
8.   The issue reopened.

About 7 minutes; safe to run twice.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent  # noqa: E402
from common import ROOT, github_api, github_app_token, service, ssm_client  # noqa: E402

SERVICE, VERSION, KEY = "orders-service", "1.4.0", "ORD-12"
COMMENT = "Earlier take"


def main_reset():
    import importlib.util

    spec = importlib.util.spec_from_file_location("shared_reset", ROOT / "scripts" / "reset.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    return shared


def production_version():
    from check import wait_for_version
    from deploy import lambda_client

    function = service(SERVICE)["environments"]["production"]["function"]
    url = lambda_client(SERVICE).get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"]
    _, seen = wait_for_version(url.rstrip("/"), VERSION, 5)
    return seen


def step_release(reset, shared):
    import history
    import host
    from common import get_parameter
    from release_job import ReleaseJob

    on_main = host.main_version(service(SERVICE)["path"])
    live = production_version()
    if on_main == VERSION and live == VERSION:
        print(f"  production runs {VERSION} and main has it: no release needed")
        return
    reset.act(f"stage {SERVICE} {VERSION} on main (main has {on_main})", lambda: host.stage(SERVICE, VERSION))
    if reset.dry_run:
        print(f"  would release {SERVICE} {VERSION} for {KEY} as a standard change, as Sam (production runs {live})")
        return
    job = ReleaseJob.as_release_bot()
    sam = get_parameter(ssm_client(), "slack/member-id/sam")
    queue = job.start({"SERVICE": SERVICE, "JIRA_KEY": KEY, "OPTION": "standard", "REQUESTED_BY": sam,
                       "SLACK_CHANNEL": "", "SLACK_TS": "", "REQUESTED_AT": str(int(time.time()))})
    number = job.started(queue, waiting=lambda why: print(f"  queued: {why}"))
    print(f"  release build #{number} started")
    build = history.watch(job, number, f"{SERVICE} {VERSION}")
    reset.actions += 1
    if build["result"] != "SUCCESS":
        raise RuntimeError(f"release build #{number} ended {build['result']}: production isn't on {VERSION}")
    print(f"  {SERVICE} {VERSION} is live, from build #{number}")


def step_pulls(reset):
    token, repository, _ = github_app_token(ssm_client())
    login = next(iter(agent.agents().values()))["github_login"]
    pulls = [p for p in github_api("GET", f"/repos/{repository}/pulls?state=open&per_page=100", token, scheme="token") or []
             if p["user"]["login"] == login]
    if not pulls:
        print("  no open pull request by the coding agent")
    for pull in pulls:
        def close(pull=pull):
            github_api("POST", f"/repos/{repository}/issues/{pull['number']}/comments", token, {"body": COMMENT}, scheme="token")
            github_api("PATCH", f"/repos/{repository}/pulls/{pull['number']}", token, {"state": "closed"}, scheme="token")

        reset.act(f"close pull request #{pull['number']} ({pull['title']}) with the comment \"{COMMENT}\"", close)


def step_issue(reset):
    import setup
    from jira import Jira

    jira = Jira()
    found = setup.find_issue(jira)
    if not found:
        raise RuntimeError("No issue \"Show the order count per store\" yet: run scripts/demos/agent_ship/setup.py once.")
    status = found["fields"]["status"]
    if status.get("statusCategory", {}).get("key") == "done" or status["name"].lower() == "done":
        reset.act(f"reopen {found['key']} ({status['name']})",
                  lambda: jira.transition(found["key"], "Backlog") or jira.transition(found["key"], "To Do"))
    else:
        print(f"  {found['key']}: {status['name']}")


def run(dry_run):
    started = time.monotonic()
    shared = main_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset video 16: production and main on {SERVICE} {VERSION} from a real release"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"3. The take's tags and GitHub releases, after {shared.BASELINE}", lambda: shared.github_takes(reset))
    reset.step(f"4. The take's release copies in JFrog, after {shared.BASELINE}", lambda: shared.jfrog_takes(reset))
    reset.step(f"5. Jira versions after {shared.BASELINE}, and {' and '.join(shared.DEMO_ISSUES)}",
               lambda: shared.jira_versions(reset))
    reset.step(f"6. {SERVICE} {VERSION} in production, from a real release", lambda: step_release(reset, shared))
    reset.step("7. The coding agent's open pull requests", lambda: step_pulls(reset))
    reset.step("8. The issue", lambda: step_issue(reset))
    import run_state

    took = run_state.duration(time.monotonic() - started)
    if reset.failed:
        print(f"Finished in {took}, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    print(f"Dry run in {took}: {reset.actions} action(s) to take." if dry_run else
          f"Ready for a take, in {took}: {reset.actions} action(s).")
    return 0


def rulesets_on(dry_run):
    """With your gh login, which may administer the repository: neither app may."""
    if dry_run:
        print("Would turn video 16's three rulesets on (ruleset.py --on).")
        return 0
    return subprocess.run([sys.executable, str(HERE / "ruleset.py"), "--on"]).returncode


def main():
    import host

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        if rulesets_on(args.dry_run) != 0:
            print("The rulesets didn't turn on: is gh signed in as the repository's owner? (gh auth status)")
            return 1
        shown = ["--dry-run"] if args.dry_run else []
        return host.hand_over("reset", "demos/agent_ship/reset.py", shown, interval=5,
                              title=" ".join(["reset.py agent-ship", *shown]))
    if host.on_host() and not args.here:
        print("On the tools host the rulesets stay as they are: turn them on from the Mac (ruleset.py --on).")
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
