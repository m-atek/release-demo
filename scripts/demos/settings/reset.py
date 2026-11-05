"""Put video 29 back where a take starts: 1.31.0 in staging and production, the setting at its baseline.

    python scripts/reset.py settings              from the Mac (hands over to the tools host) or the reset job
    python scripts/reset.py settings --dry-run    every action it would take, and nothing else

1. the take's runs stopped (release, rollback and setting), open orders-service changes cancelled;
2. the demos' switches off, so 1.31.0's own release has no extra stage;
3. ORD-[s], the one Jira story, made if it doesn't exist, and printed;
4. the first time, a real release of 1.31.0 under ORD-[s] as a standard change (about 4 minutes);
5. 1.31.0 on main;
6. both aliases on the newest published 1.31.0, and both functions called once;
7. the setting at its baseline in both environments: a running deployment stopped first, and a
   deployment, all at once with no bake time, only where the value differs;
8. the Slack handler warmed.

Under 2 minutes after the first time, safe to run twice. A refused version stays in AWS AppConfig's
store: nothing deploys it, and each change starts from what production last completed.
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(SCRIPTS))

import host  # noqa: E402
import setting  # noqa: E402
from common import fail, get_parameter, service, ssm_client  # noqa: E402

SERVICE, BASELINE = "orders-service", "1.31.0"
STORY = "Show the free delivery date on the order page"
STORY_TEXT = "The order page says until when delivery is free, from a setting that changes without a release."
DESCRIPTION = "reset.py before the next take"


def shared_reset():
    """scripts/reset.py as a module, pointed at this video's version: this file has its name. 1.31.0 isn't
    archived: it's the baseline, and no take makes a tag, a package or a Jira version."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASELINE = BASELINE
    module.JOBS = [*module.JOBS, "setting"]
    return module


def story_key(reset, jira=None):
    """ORD-[s]: found by its exact summary, or made."""
    from jira import PROJECT, Jira

    jira = jira or Jira()
    found = jira.search(f'project = {PROJECT} AND summary ~ "\\"{STORY}\\"" ORDER BY key', fields="summary")
    key = next((issue["key"] for issue in found if issue["fields"]["summary"] == STORY), None)
    if key:
        print(f"  ORD-[s] is {key}: {STORY}")
        return key
    made = {}
    reset.act(f"make the Jira story \"{STORY}\"", lambda: made.update(key=jira.create("Story", STORY, STORY_TEXT, labels=[SERVICE])))
    if made:
        print(f"  ORD-[s] is {made['key']}")
    return made.get("key", "ORD-?")


def has_baseline():
    """Does production have a Lambda version of 1.31.0?"""
    from deploy import lambda_client

    client = lambda_client(SERVICE)
    function = service(SERVICE)["environments"]["production"]["function"]
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        if any(version.get("Description") == BASELINE for version in page["Versions"]):
            return True
    return False


def release_baseline(reset, key):
    """The first time: a real release of 1.31.0, standard, as history.py starts one, with Sam as the requester."""
    from history import release, watch
    from release_job import ReleaseJob

    if reset.dry_run:
        print(f"  would stage {SERVICE} {BASELINE} and release it under {key} as a standard change, "
              "as release-bot with Sam as the requester, and wait for it")
        reset.actions += 1
        return
    job = ReleaseJob.as_release_bot()
    number = release(job, BASELINE, key, get_parameter(ssm_client(), "slack/member-id/sam"))
    build = watch(job, number, f"{SERVICE} {BASELINE} for {key}")
    reset.actions += 1
    if build["result"] != "SUCCESS":
        fail(f"The release of {BASELINE}, build #{number}, ended {build['result']}. Look at it in Jenkins.")
    print(f"  {SERVICE} {BASELINE} is live from build #{number}")


def stage(reset):
    current = host.main_version(service(SERVICE)["path"])
    if current == BASELINE:
        print(f"  main has {SERVICE} {BASELINE}")
        return
    reset.act(f"stage {SERVICE} {BASELINE} on main (main has {current})", lambda: host.stage(SERVICE, BASELINE))


def put_at_baseline(client, known, environment, content, baseline):
    made = client.create_hosted_configuration_version(
        ApplicationId=known["application"], ConfigurationProfileId=known["profile"],
        Content=json.dumps({**content, **baseline}, indent=2).encode(), ContentType="application/json",
        LatestVersionNumber=setting.flags.latest_version(client, known), Description=DESCRIPTION)
    number = setting.start(client, known, environment, made["VersionNumber"], "quick", DESCRIPTION)
    setting.wait_done(client, known, environment, number, setting.DEPLOY_SECONDS)


def settings_at_baseline(reset, client=None, known=None):
    client, known = client or setting.appconfig(), known or setting.ids(SERVICE)
    baseline = setting.settings_of(SERVICE)["baseline"]
    for environment in ("staging", "production"):
        going = setting.flags.running(client, known, environment)
        if going:
            reset.act(f"stop AWS AppConfig deployment {going} in {environment}",
                      lambda environment=environment, going=going: setting.flags.stop(client, known, environment, going))
        _, content = setting.current(client, known, environment)
        if all(content.get(key) == value for key, value in baseline.items()):
            print(f"  {environment}: {baseline}")
            continue
        reset.act(f"put {environment} back on {baseline} (all at once, no bake time)",
                  lambda environment=environment, content=content: put_at_baseline(client, known, environment, content, baseline))


def run(dry_run, slack_url):
    started = time.monotonic()
    shared = shared_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset video 29: {SERVICE} {BASELINE} in staging and production, the setting at its baseline"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step("3. The demos' switches", lambda: shared.switches_off(reset))
    keys = {}
    reset.step("4. The Jira story", lambda: keys.update(s=story_key(reset)))
    first_time = False

    def production():
        nonlocal first_time
        first_time = not has_baseline()
        if first_time:
            if "s" not in keys:
                raise RuntimeError("the story wasn't read, so there is no issue to release 1.31.0 under")
            release_baseline(reset, keys["s"])
        else:
            print(f"  production has a Lambda version of {BASELINE}")

    reset.step(f"5. {BASELINE} in production", production)
    if reset.failed and first_time:
        print(f"Stopped: {'; '.join(reset.failed)}.")
        return 1
    reset.step(f"6. {SERVICE} {BASELINE} on main", lambda: stage(reset))
    if first_time and dry_run:
        print("7. Lambda aliases, and one call each\n  skipped in a dry run: the release would make the version first")
    else:
        reset.step("7. Lambda aliases, and one call each", lambda: shared.lambda_aliases(reset))
    reset.step("8. The setting in AWS AppConfig", lambda: settings_at_baseline(reset))
    reset.step("9. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))
    took = time.monotonic() - started
    if reset.failed:
        print(f"Finished in {took:.0f}s, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    print(f"{'Dry run' if dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s)"
          f"{' to take' if dry_run else ''}. {'' if dry_run else 'Ready for a take.'}".rstrip())
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
        return host.hand_over("reset", "demos/settings/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py settings", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
