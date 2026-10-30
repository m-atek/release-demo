"""Put demo 21 back where a take starts: 1.4.0 in production, the flag off, 1.8.0 staged.

    python scripts/reset.py release-train              from the Mac (hands over to the tools host) or the reset job
    python scripts/reset.py release-train --dry-run    every action it would take, and nothing else

1. the take's release runs stopped, open orders-service changes cancelled;
2. the main reset's archiving for 1.8.0: its tag and GitHub release under
   takes/, the JFrog release copy under takes/, the Jira version "orders-service
   1.8.0" un-released;
3. ORD-[u] and ORD-[v] Done, ORD-[w] In Progress;
4. both aliases on the newest published 1.4.0 version;
5. delivery_estimate_v2 off in both environments: a running deployment is
   stopped first, and a deployment (all at once, no bake time, its description
   naming reset.py) only where the flag is on;
6. 1.8.0 staged on main;
7. both functions called once, and the Slack handler warmed.

Under two minutes, safe to run twice.
"""

import argparse
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(1, str(SCRIPTS))

import flags  # noqa: E402
import host  # noqa: E402
from common import service  # noqa: E402

SERVICE, BASELINE, STATE, FLAG = "orders-service", "1.4.0", "1.8.0", "delivery_estimate_v2"
DESCRIPTION = "reset.py before the next take"


def shared_reset():
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASELINE = BASELINE
    # Only what this demo releases: 1.8.0. Other videos' versions stay where their resets put them.
    module.after_baseline = lambda version: version == STATE
    return module


def jira_issues(reset):
    from setup import ISSUES
    from jira import PROJECT, Jira

    jira = Jira()
    for letter, (summary, status, _) in ISSUES.items():
        found = [i for i in jira.search(f'project = {PROJECT} AND summary ~ "\\"{summary}\\""', fields="summary,status")
                 if i["fields"]["summary"] == summary]
        if not found:
            raise RuntimeError(f'No Jira issue "{summary}" yet: run scripts/demos/release-train/setup.py once.')
        key, now = found[0]["key"], found[0]["fields"]["status"]["name"]
        if now.lower() == status.lower():
            print(f"  {key}: {now}")
            continue
        reset.act(f"move {key} from {now} to {status}", lambda key=key, status=status: jira.transition(key, status))


def aliases(reset):
    import deploy

    client = deploy.lambda_client(SERVICE)
    for name, environment in service(SERVICE)["environments"].items():
        function = environment["function"]
        versions = []
        for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
            versions += [v["Version"] for v in page["Versions"] if v.get("Description") == BASELINE]
        if not versions:
            raise RuntimeError(f"{function} has no published {BASELINE}: release it once (the main demo's take 1).")
        target = max(versions, key=int)
        live = deploy.alias_version(client, function)
        if live == target:
            print(f"  {function}: live is on version {target} ({BASELINE})")
            continue
        reset.act(f"move {function} live from version {live} to {target} ({BASELINE})",
                  lambda function=function, target=target: deploy.move_alias(client, function, target))


def flag_off(reset):
    known = flags.ids()
    appconfig = flags.client()
    for environment in ("staging", "production"):
        going = flags.running(appconfig, known, environment)
        if going:
            reset.act(f"stop AWS AppConfig deployment {going} in {environment}",
                      lambda environment=environment, going=going: flags.stop(appconfig, known, environment, going))
        # The configuration in effect once nothing runs: the last completed deployment's.
        if not flags.current_values(appconfig, known, environment).get(FLAG, False):
            print(f"  {environment}: {FLAG} is off")
            continue
        reset.act(f"switch {FLAG} off in {environment} (all at once, no bake time)",
                  lambda environment=environment: flags.switch(environment, FLAG, False, DESCRIPTION))


def stage(reset):
    current = host.main_version(service(SERVICE)["path"])
    if current == STATE:
        print(f"  main has {SERVICE} {STATE}")
        return
    reset.act(f"stage {SERVICE} {STATE} on main (main has {current})", lambda: host.stage(SERVICE, STATE))


def warm(reset):
    import deploy
    from check import wait_for_version
    from common import http_get

    client = deploy.lambda_client(SERVICE)
    for environment in service(SERVICE)["environments"].values():
        function = environment["function"]
        url = client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"].rstrip("/")
        if reset.dry_run:
            print(f"  would call {function} once")
            continue
        live, seen = wait_for_version(url, BASELINE, 30)
        status, _ = http_get(url + "/", timeout=15)
        print(f"  {function}: /health reports {seen or 'nothing'}, the page answered {status or 'nothing'}")
        if not live or status != 200:
            raise RuntimeError(f"{function} doesn't serve {BASELINE} yet.")


def run(dry_run, slack_url):
    started = time.monotonic()
    shared = shared_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset demo 21: {SERVICE} {BASELINE} in staging and production, {FLAG} off, {STATE} staged"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Release runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"3. {STATE}'s tag and GitHub release", lambda: shared.github_takes(reset))
    reset.step(f"4. {STATE}'s release copy in JFrog", lambda: shared.jfrog_takes(reset))

    def jira():
        from jira import Jira

        version = Jira().version(f"{SERVICE} {STATE}", create=False)
        if version and version.get("released"):
            reset.act(f"un-release the Jira version {SERVICE} {STATE}", lambda: Jira().call(
                "PUT", f"/rest/api/3/version/{version['id']}", {"released": False}))
        else:
            print(f"  {SERVICE} {STATE}: not released")
        jira_issues(reset)

    reset.step(f"5. The Jira version {SERVICE} {STATE} and its three issues", jira)
    reset.step(f"6. Lambda aliases on {BASELINE}", lambda: aliases(reset))
    reset.step(f"7. {FLAG} off in AWS AppConfig", lambda: flag_off(reset))
    reset.step(f"8. {SERVICE} {STATE} on main", lambda: stage(reset))
    reset.step("9. Both functions, one call each", lambda: warm(reset))
    reset.step("10. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))
    took = time.monotonic() - started
    if reset.failed:
        print(f"Finished in {took:.0f}s, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    print(f"{'Dry run' if dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s). "
          f"{'' if dry_run else 'Ready for a take.'}".rstrip())
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--slack-handler-url", help="the Slack handler's function URL; from the Mac, looked up")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        url = args.slack_handler_url or shared_reset().slack_handler_url()
        shown = ["--dry-run"] if args.dry_run else []
        return host.hand_over("reset", "demos/release-train/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py release-train", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
