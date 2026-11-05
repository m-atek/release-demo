"""Put video 28 back where a take starts: 1.26.0 in staging and production, 1.30.0 on main, every switch off.

    python scripts/reset.py two-ways              from the Mac (hands over to the tools host) or the reset job
    python scripts/reset.py two-ways --dry-run    every action it would take, and nothing else

The shared steps, pointed at this video's version only, so other videos' stay where their resets put them. Neither take
goes live, so a retake needs none of this: send the command again and it makes a new build. The reset is for the first
take of the day, and for a take that went live by mistake.

1. the take's release runs stopped, open orders-service changes cancelled;
2. 1.30.0's tag, GitHub release and JFrog release copy archived under takes/, if a take ever made them, and its Jira
   version un-released;
3. ORD-[s] made if it doesn't exist yet, and reopened;
4. 1.30.0 staged on main;
5. the demos' switches off, as the main reset turns them off;
6. both aliases on the newest Lambda version of 1.26.0, video 27's clean baseline, and each function called once;
7. the Slack handler warmed.

Under 2 minutes, safe to run twice.
"""

import argparse
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(1, str(SCRIPTS))

import host  # noqa: E402
import run_state  # noqa: E402
from common import service  # noqa: E402

SERVICE, BASELINE, STAGED = "orders-service", "1.26.0", "1.30.0"
TAKE_VERSIONS = {STAGED}
STORY = "Offer gift wrap on the order page"
STORY_TEXT = "One line on the order page: gift wrap on any order, for a fixed price."
SCRIPT = "demos/two_ways/reset.py"


def shared_reset():
    """scripts/reset.py as a module, pointed at this video's version: this file has its name."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASELINE = BASELINE
    module.after_baseline = lambda version: version in TAKE_VERSIONS
    return module


def find_story(jira):
    from jira import PROJECT

    found = jira.search(f'project = {PROJECT} AND summary ~ "\\"{STORY}\\"" ORDER BY key', fields="summary,status")
    return next((issue for issue in found if issue["fields"]["summary"] == STORY), None)


def story(reset, shared, jira=None):
    """ORD-[s]: made once, as reset.py patch-clock makes its issues, so the reset alone brings a take up."""
    if jira is None:
        from jira import Jira

        jira = Jira()
    found = find_story(jira)
    if found:
        print(f"  ORD-[s] is {found['key']}: {STORY}")
        shared.DEMO_ISSUES = [found["key"]]
        shared.jira_versions(reset)
        return found["key"]
    if reset.dry_run:
        print(f"  would make the Story \"{STORY}\", with the next free key")
        return None
    key = jira.create("Story", STORY, STORY_TEXT, labels=[SERVICE], fix_version=f"{SERVICE} {STAGED}")
    reset.actions += 1
    print(f"  ORD-[s] is {key}: {STORY} (made now)")
    return key


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
    print(f"Reset video 28: {SERVICE} {BASELINE} in staging and production, {STAGED} on main, every switch off"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    key = []

    def jira():
        key.append(story(reset, shared))

    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"3. {STAGED}'s tag and GitHub release", lambda: shared.github_takes(reset))
    reset.step(f"4. {STAGED}'s release copy in JFrog", lambda: shared.jfrog_takes(reset))
    reset.step("5. ORD-[s] and its Jira version", jira)
    reset.step(f"6. {SERVICE} {STAGED} on main", lambda: stage(reset))
    reset.step("7. The demos' switches", lambda: shared.switches_off(reset))
    reset.step(f"8. Lambda aliases on {BASELINE}, and one call each", lambda: shared.lambda_aliases(reset))
    reset.step("9. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))

    took = run_state.duration(time.monotonic() - started)
    if reset.failed:
        print(f"Finished in {took}, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    if dry_run:
        print(f"Dry run in {took}: {reset.actions} action(s) to take.")
        return 0
    print(f"Reset in {took}: {reset.actions} action(s).")
    print(f"Ready for a take: production on {BASELINE}, {STAGED} on main, ORD-[s] is {key[0] if key else '?'}.")
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
        return host.hand_over("reset", SCRIPT, [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py two-ways", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
