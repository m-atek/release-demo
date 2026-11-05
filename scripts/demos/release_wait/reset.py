"""Put video 31 back where its first take starts: 1.26.0 in staging and production, 1.33.0 staged, every demo's switch off.

    python scripts/reset.py release-wait              from the Mac (hands over to the tools host) or the reset job
    python scripts/reset.py release-wait --dry-run    every action it would take, and nothing else

The shared steps, pointed at this video's versions only, so other videos' stay
where their resets put them:

1. a release run still going stopped, open orders-service changes cancelled;
2. the tags, GitHub releases and JFrog copies of 1.33.0 and 1.33.1 archived under
   takes/, their Jira versions un-released;
3. ORD-[a] and ORD-[b] made if setup.py hasn't run, and reopened;
4. 1.33.0 staged on main;
5. both aliases on the newest Lambda version of 1.26.0, which video 27's reset
   released, and both functions called once;
6. the Slack handler warmed;
7. the demos' switches off, so the release shows the shared twelve stages.

Under two minutes, safe to run twice. Run it before a take, never during one: this
reset, like every demo's, stops a release run that is waiting for its approval, and
that is a day's wait gone.
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
from common import service  # noqa: E402

SERVICE, BASELINE, STAGED = "orders-service", "1.26.0", "1.33.0"
TAKE_VERSIONS = {"1.33.0", "1.33.1"}


def shared_reset():
    """scripts/reset.py as a module, pointed at this video's versions: this file has its name."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASELINE = BASELINE
    module.after_baseline = lambda version: version in TAKE_VERSIONS
    return module


def stories(dry_run=False):
    """{letter: key}. Makes ORD-[a] and ORD-[b] if setup.py hasn't run: on the day the reset is what runs by itself."""
    import setup as wait_setup
    from jira import Jira

    jira = Jira()
    wait_setup.stories(jira, dry_run)
    return {letter: wait_setup.find_story(jira, story[0]) for letter, story in wait_setup.STORIES.items()}


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
    print(f"Reset video 31: {SERVICE} {BASELINE} in staging and production, {STAGED} staged, every switch off"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")

    def jira():
        keys = stories(dry_run)
        shared.DEMO_ISSUES = [key for key in (keys["a"], keys["b"]) if key]
        if shared.DEMO_ISSUES:
            shared.jira_versions(reset)

    try:
        reset.step("1. Release runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step("3. The takes' tags and GitHub releases", lambda: shared.github_takes(reset))
    reset.step("4. The takes' release copies in JFrog", lambda: shared.jfrog_takes(reset))
    reset.step("5. Jira: the versions, ORD-[a] and ORD-[b]", jira)
    reset.step(f"6. {SERVICE} {STAGED} on main", lambda: stage(reset))
    reset.step("7. Lambda aliases, and one call each", lambda: shared.lambda_aliases(reset))
    reset.step("8. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))
    reset.step("9. The demos' switches", lambda: shared.switches_off(reset))
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
        return host.hand_over("reset", "demos/release_wait/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py release-wait", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
