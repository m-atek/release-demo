"""reset.py ai-notes: where videos 12 and 13 start. Production on 1.4.0 from a
real release, 1.4.1 staged on main, and the AI notes switch on.

    python scripts/reset.py ai-notes              from the Mac it runs itself on the tools host, as the main reset does
    python scripts/reset.py ai-notes --dry-run    every action it would take, and nothing else
    or the Jenkins job reset with DEMO=ai-notes

1. When production already runs 1.4.0 from a real release made since the
   last main reset (live is a version of 1.4.0 whose package is the release
   copy in orders-service-releases/1.4.0/), it skips the release and frees
   only what a take made: the main reset's steps with 1.4.0 as the baseline.
   That cancels open changes, archives the takes' tags and release copies
   after 1.4.0, un-releases their Jira versions and reopens ORD-13, and puts
   both aliases on 1.4.0. Under 2 minutes.
2. Otherwise the main reset, then a real release of 1.4.0 under ORD-12 as a
   standard change, started the way history.py starts one: 1.4.0 staged on
   main, the release job as release-bot with Sam as the requester, its own
   message in #releases. About 6 minutes in all.
3. 1.4.1 staged on main, and the switch on.

Safe to run twice: the second run takes step 1. The real release step is
shared with reset.py cost-gate (video 18), which needs its cost check too.
"""

import argparse
import importlib.util
import json
import sys
import time

import notes  # noqa: F401  (puts scripts/ on the path)
import host
import run_state
from common import ROOT, SSM_PREFIX, fail, get_parameter, ssm_client

SERVICE = "orders-service"
BASELINE = "1.4.0"
RELEASE_KEY = "ORD-12"
STAGED = "1.4.1"
TAKE_ISSUES = ["ORD-13"]
SWITCH = "ai-notes/enabled"


def main_reset(baseline=None, issues=None):
    """scripts/reset.py as a module, its steps pointed at a baseline and the issues to reopen."""
    spec = importlib.util.spec_from_file_location("shared_reset", ROOT / "scripts" / "reset.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    if baseline:
        shared.BASELINE = baseline
    if issues is not None:
        shared.DEMO_ISSUES = issues
    return shared


def live_hex():
    """Production's live Lambda version and the SHA-256 of its package, as hex."""
    from deploy import alias_version, app_version, lambda_client
    from evidence import base64_to_hex

    client = lambda_client(SERVICE)
    function = notes.service(SERVICE)["environments"]["production"]["function"]
    number = alias_version(client, function)
    code = client.get_function_configuration(FunctionName=function, Qualifier=number)["CodeSha256"]
    return number, app_version(client, function, number), base64_to_hex(code)


def release_copy(need_cost=False):
    """The JFrog folder of production's release copy of the baseline, if production
    runs one (with its cost.json, when need_cost): None otherwise."""
    import publish

    number, version, sha256 = live_hex()
    if version != BASELINE:
        return None
    token = get_parameter(ssm_client(), "jcr/token")
    root = f"{publish.releases_root(SERVICE)}/{BASELINE}"
    status, body = publish.request("GET", f"api/storage/{root}", token)
    if status != 200:
        return None
    folders = [root] + [f"{root}/{child['uri'].strip('/')}" for child in json.loads(body).get("children", [])
                        if child.get("folder")]
    for folder in folders:
        status, info = publish.request("GET", f"{folder}/build-info.json", token)
        if status == 200 and json.loads(info).get("sha256") == sha256:
            if need_cost and not publish.stored_sha256(token, f"{folder}/cost.json"):
                return None
            return folder
    return None


def released(need_cost=False):
    try:
        return release_copy(need_cost)
    except BaseException as error:  # fail() exits; unknown means do the full reset
        print(f"  can't tell what production runs ({error.__class__.__name__}): the full reset follows")
        return None


def release_baseline(reset):
    """The real release of 1.4.0, standard, under ORD-12, the way history.py starts one."""
    from history import release, watch
    from release_job import ReleaseJob

    if reset.dry_run:
        print(f"  would stage {SERVICE} {BASELINE} and release it under {RELEASE_KEY} as a standard change, "
              "as release-bot with Sam as the requester, and wait for it")
        reset.actions += 1
        return
    job = ReleaseJob.as_release_bot()
    started = time.monotonic()
    number = release(job, BASELINE, RELEASE_KEY, get_parameter(ssm_client(), "slack/member-id/sam"))
    build = watch(job, number, f"{SERVICE} {BASELINE} for {RELEASE_KEY}")
    reset.actions += 1
    if build["result"] != "SUCCESS":
        fail(f"The release of {BASELINE}, build #{number}, ended {build['result']}. Look at it in Jenkins.")
    print(f"  {SERVICE} {BASELINE} is live from build #{number}, after {run_state.duration(time.monotonic() - started)}")


def set_switch(reset, name, value):
    ssm = ssm_client()
    try:
        current = ssm.get_parameter(Name=SSM_PREFIX + name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        fail(f"{SSM_PREFIX}{name} doesn't exist yet. Apply infra/ first (scripts/tf.sh apply).")
    if current == value:
        print(f"  {name}: {value}")
        return
    reset.act(f"turn {name} {value}", lambda: ssm.put_parameter(Name=SSM_PREFIX + name, Value=value, Type="String",
                                                                Overwrite=True))


def set_switches(reset, switches):
    for name, value in switches.items():
        set_switch(reset, name, value)


def free_take(reset, shared, slack_url=None):
    """The main reset's steps with the baseline as their starting point."""
    try:
        reset.step("Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        fail(str(error))
    reset.step("Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"The takes' tags and GitHub releases, after {shared.BASELINE}", lambda: shared.github_takes(reset))
    reset.step(f"The takes' release copies in JFrog, after {shared.BASELINE}", lambda: shared.jfrog_takes(reset))
    reset.step(f"Jira versions after {shared.BASELINE}, and {', '.join(shared.DEMO_ISSUES)}",
               lambda: shared.jira_versions(reset))
    reset.step("Lambda aliases, and one call each", lambda: shared.lambda_aliases(reset))
    reset.step("The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))


def prepare(dry_run, staged, issues, switches, need_cost=False, slack_url=None, title="ai-notes"):
    """Production on a real release of the baseline, `staged` on main, `switches` as
    given. A switch other than the AI notes is on during the release too (reset.py
    cost-gate: its cost check measures 1.4.0). Returns the exit code."""
    started = time.monotonic()
    shared = main_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset {title}: production on {SERVICE} {BASELINE} from a real release, {staged} staged"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    folder = released(need_cost)
    if folder:
        print(f"Production runs {BASELINE} from its release copy {folder}: no new release.")
        free_take(reset, main_reset(BASELINE, issues), slack_url)
    else:
        print(f"Production doesn't run a release copy of {BASELINE}{' with its cost check' if need_cost else ''}: "
              "the main reset, then a real release.")
        code = shared.reset_main(dry_run, slack_url)
        if code != 0:
            return code
        during = {name: value for name, value in switches.items() if name != SWITCH and value == "on"}
        if during:
            reset.step("Switches for the release", lambda: set_switches(reset, during))
        reset.step(f"A real release of {BASELINE} under {RELEASE_KEY}", lambda: release_baseline(reset))
        if reset.failed:
            print(f"Stopped: {'; '.join(reset.failed)}.")
            return 1
        reset.step(f"Jira: reopen {', '.join(issues)}", lambda: main_reset(BASELINE, issues).jira_versions(reset))
    reset.step(f"{SERVICE} {staged} on main", lambda: reset.act(f"stage {SERVICE} {staged} on main",
                                                                 lambda: host.stage(SERVICE, staged)))
    reset.step("Switches", lambda: set_switches(reset, switches))
    took = run_state.duration(time.monotonic() - started)
    if reset.failed:
        print(f"Finished in {took}, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    print(f"{'Dry run' if dry_run else 'Reset'} in {took}: {reset.actions} action(s). "
          f"{'' if dry_run else f'Ready for a take: production on {BASELINE}, {staged} staged.'}")
    return 0


def main(script="demos/ai_notes/reset.py", unit="reset-ai-notes", **kwargs):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--follow", action="store_true", help="from the Mac: show the run on the tools host")
    parser.add_argument("--slack-handler-url", help="the Slack handler's function URL; from the Mac, looked up")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        url = args.slack_handler_url or (None if args.follow else main_reset().slack_handler_url())
        return host.hand_over(unit, script, [*(["--dry-run"] if args.dry_run else []),
                                             *(["--slack-handler-url", url] if url else [])],
                              follow_only=args.follow, interval=5)
    sys.stdout.reconfigure(line_buffering=True)
    options = {"staged": STAGED, "issues": TAKE_ISSUES, "switches": {SWITCH: "on"}, **kwargs}
    return prepare(args.dry_run, slack_url=args.slack_handler_url, **options)


if __name__ == "__main__":
    sys.exit(main())
