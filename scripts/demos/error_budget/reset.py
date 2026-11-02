"""Put video 27 back where a take starts: 1.26.0 in production, 1.27.0 staged, the check on.

    python scripts/reset.py error-budget              from the Mac (hands over to the tools host) or the reset job
    python scripts/reset.py error-budget --dry-run    every action it would take, and nothing else

The shared steps, pointed at this video's versions only, so other videos' stay
where their resets put them:

1. the take's release runs stopped, open orders-service changes cancelled;
2. the tags, GitHub releases and JFrog copies of 1.27.0, 1.27.1, 1.28.0 and
   1.28.1 archived under takes/, their Jira versions un-released;
3. ORD-[a] and ORD-[f] reopened, any open error-budget issue closed with a comment;
4. both aliases on the newest Lambda version of 1.26.0. The first time, with the
   switch off, a real release of 1.26.0 under ORD-[p] as a standard change, as
   reset.py ai-notes does for 1.4.0 (about 6 minutes);
5. 1.27.0 staged on main;
6. the check's switch on, the AI notes' and the cost check's off;
7. both functions called once, and the Slack handler warmed;
8. the quiet day seeded, up to the minute this ends (seed.py).

Under 2 minutes after the first time, safe to run twice. Everything before the
end of a reset reads as the seeded day, so a fill can't age out, and a failed
take's failures don't wait 24 hours: the next take starts on a quiet day. From
the end of the reset on, every request and failure is real, so the traffic must
be on by then. It has its own switch, which no reset touches: step 8 stops with a
failure if it is off, and scripts/demo.sh up error-budget turns it on.
"""

import argparse
import datetime
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(1, str(SCRIPTS))

import host  # noqa: E402
from common import SSM_PREFIX, aws_session, fail, get_parameter, service, ssm_client  # noqa: E402

SERVICE, BASELINE, STAGED = "orders-service", "1.26.0", "1.27.0"
TAKE_VERSIONS = {"1.27.0", "1.27.1", "1.28.0", "1.28.1"}
SWITCHES = {"error-budget/enabled": "on", "ai-notes/enabled": "off", "cost-check/enabled": "off"}
TAKE_COMMENT = "Earlier take"
TRAFFIC = "error-budget/traffic-until"


def shared_reset():
    """scripts/reset.py as a module, pointed at this video's versions: this file has its name."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASELINE = BASELINE
    module.after_baseline = lambda version: version in TAKE_VERSIONS
    return module


def load_seed():
    """seed.py by its path: scripts/demos has more than one."""
    spec = importlib.util.spec_from_file_location("budget_seed", HERE / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cloudwatch_client():
    return aws_session().client("cloudwatch")


def stories():
    from jira import Jira
    from setup import story_keys

    return story_keys(Jira())


def close_budget_issues(reset, jira=None):
    """Any open issue with the budget's label: closed, with a comment, so the next refusal opens its own."""
    from jira import PROJECT, Jira, paragraph

    jira = jira or Jira()
    label = service(SERVICE)["error_budget"]["issue_label"]
    found = jira.search(f'project = {PROJECT} AND labels = "{label}" AND statusCategory != Done ORDER BY key', fields="summary")
    if not found:
        print("  no open error-budget issue")
    for issue in found:
        def close(key=issue["key"]):
            jira.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": paragraph(TAKE_COMMENT)})
            jira.transition(key, "Done")

        reset.act(f"move {issue['key']} to Done with the comment \"{TAKE_COMMENT}\"", close)


def has_baseline():
    """Does production have a Lambda version of the baseline?"""
    from deploy import lambda_client

    client = lambda_client(SERVICE)
    function = service(SERVICE)["environments"]["production"]["function"]
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        if any(version.get("Description") == BASELINE for version in page["Versions"]):
            return True
    return False


def set_switches(reset, wanted, ssm=None):
    ssm = ssm or ssm_client()
    for name, value in wanted.items():
        try:
            current = ssm.get_parameter(Name=SSM_PREFIX + name)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            fail(f"{SSM_PREFIX}{name} doesn't exist yet. Apply infra/ first (scripts/tf.sh apply).")
        if current == value or (value == "off" and current != "on"):
            print(f"  {name}: {value}")
            continue
        reset.act(f"turn {name} {value}", lambda name=name, value=value: ssm.put_parameter(
            Name=SSM_PREFIX + name, Value=value, Type="String", Overwrite=True))


def traffic_is_on(ssm=None, now=None):
    """The take's own minutes hold what the load generator sends, so it must be on."""
    ssm = ssm or ssm_client()
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + TRAFFIC)["Parameter"]["Value"]
        return datetime.datetime.fromisoformat(value).timestamp() > (now or time.time())
    except (ssm.exceptions.ParameterNotFound, ValueError):
        return False


def seed_fill(reset, now=None):
    """The quiet day, up to this minute. Only with the traffic on: from here on, nothing is seeded."""
    settings = service(SERVICE)["error_budget"]
    if not settings.get("seed"):
        print("  services.yaml names no seed: every minute is what AWS/Lambda counted")
        return
    if not traffic_is_on(now=now):
        message = "the traffic is off, so the take's minutes would hold no requests: scripts/demo.sh up error-budget, then this again"
        if not reset.dry_run:
            raise RuntimeError(message)
        print(f"  {message}")
    seed = load_seed()
    minutes = seed.minutes_to_seed(now or time.time(), settings["window_hours"])
    reset.act(seed.describe(minutes), lambda: seed.write(cloudwatch_client(), settings, minutes))


def release_baseline(reset, key):
    """The first time: a real release of 1.26.0, standard, with every switch off, as history.py starts one."""
    from history import release, watch
    from release_job import ReleaseJob

    set_switches(reset, dict.fromkeys(SWITCHES, "off"))
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
    if current == STAGED:
        print(f"  main has {SERVICE} {STAGED}")
        return
    reset.act(f"stage {SERVICE} {STAGED} on main (main has {current})", lambda: host.stage(SERVICE, STAGED))


def run(dry_run, slack_url):
    started = time.monotonic()
    shared = shared_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset video 27: {SERVICE} {BASELINE} in staging and production, {STAGED} staged, the check on"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    keys = {}

    def jira():
        keys.update(stories())
        shared.DEMO_ISSUES = [keys["a"], keys["f"]]
        shared.jira_versions(reset)
        close_budget_issues(reset)

    try:
        reset.step("1. Release runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step("3. The takes' tags and GitHub releases", lambda: shared.github_takes(reset))
    reset.step("4. The takes' release copies in JFrog", lambda: shared.jfrog_takes(reset))
    reset.step("5. Jira: the versions, ORD-[a] and ORD-[f], and any open error-budget issue", jira)
    first_time = False

    def production():
        nonlocal first_time
        first_time = not has_baseline()
        if first_time:
            if "p" not in keys:
                raise RuntimeError("the stories weren't read, so there is no issue to release 1.26.0 under")
            release_baseline(reset, keys["p"])

    reset.step(f"6. {BASELINE} in production", production)
    if reset.failed and first_time:
        print(f"Stopped: {'; '.join(reset.failed)}.")
        return 1
    reset.step(f"7. {SERVICE} {STAGED} on main", lambda: stage(reset))
    reset.step("8. The switches", lambda: set_switches(reset, SWITCHES))
    if first_time and dry_run:
        print("9. Lambda aliases, and one call each\n  skipped in a dry run: the release would make the version first")
    else:
        reset.step("9. Lambda aliases, and one call each", lambda: shared.lambda_aliases(reset))
    reset.step("10. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))
    reset.step("11. The seeded fill, up to now", lambda: seed_fill(reset))
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
        return host.hand_over("reset", "demos/error_budget/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py error-budget", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
