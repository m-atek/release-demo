"""Put video 30 back where a take starts: production on 1.3.0, 1.32.0 on main with today's pins.

    python scripts/reset.py young-dependency              from the Mac (hands over to the tools host) or the reset job
    python scripts/reset.py young-dependency --dry-run    every action it would take, and nothing else

The shared steps, pointed at this video's versions only, so other videos' stay
where their resets put them:

1. the take's release runs stopped, open orders-service changes cancelled;
2. the tags, GitHub releases and JFrog copies of 1.32.0 and 1.32.1 archived under
   takes/, their Jira versions un-released;
3. ORD-[u] made if it doesn't exist, reopened if it does;
4. the other demos' switches off, and /release on Jenkins;
5. the package: pick.py reads PyPI now. If nothing on the watch list is young enough,
   this stops here and says so, and nothing is staged: the take waits;
6. 1.32.0 on main, with that package's newest version pinned, in one commit;
7. both aliases on 1.3.0, both functions called once, the Slack handler warmed.

Under 2 minutes, safe to run twice: a second run only moves the pins if a newer version
is out. It has no switch of its own: the stage comes with the versions services.yaml names.
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
import pick  # noqa: E402
from common import SSM_PREFIX, ssm_client  # noqa: E402

SERVICE, BASELINE, STAGED = "orders-service", "1.3.0", pick.REFUSED_VERSION
TAKE_VERSIONS = {pick.REFUSED_VERSION, pick.PASSING_VERSION}
SUMMARY = "Pin the packages of orders-service at current versions"
DESCRIPTION = "Pin the AWS SDK and what it brings, instead of taking the copy Lambda ships."
LABEL = "package-age"
NAME = "ORD-[u]"   # as the script calls it


def shared_reset():
    """scripts/reset.py as a module, pointed at this video's versions: this file has its name."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASELINE = BASELINE
    module.after_baseline = lambda version: version in TAKE_VERSIONS
    return module


def find_issue(jira):
    from jira import PROJECT

    found = jira.search(f'project = {PROJECT} AND labels = {LABEL} AND summary ~ "\\"{SUMMARY}\\"" ORDER BY key', fields="summary,status")
    return next((issue["key"] for issue in found if issue["fields"]["summary"] == SUMMARY), None)


def jira_step(reset, shared, jira=None):
    """ORD-[u], and the versions. The key goes on the take card."""
    from jira import Jira

    jira = jira or Jira()
    key = find_issue(jira)
    if not key:
        if reset.dry_run:
            print(f"  would make {NAME}: {SUMMARY}")
            reset.actions += 1
            return None
        key = jira.create("Story", SUMMARY, DESCRIPTION, labels=[LABEL])
        reset.actions += 1
    print(f"  {NAME} is {key}")
    shared.DEMO_ISSUES = [key]
    shared.jira_versions(reset)
    return key


def on_jenkins(ssm=None):
    """What /release orders-service starts, as the Slack handler reads it: jenkins, unless demo 17 or 22 is up."""
    found = (ssm or ssm_client()).get_parameters(Names=[f"{SSM_PREFIX}ci/orders-service"])["Parameters"]
    value = found[0]["Value"] if found else ""
    if value in ("gitlab-ci", "azure-pipelines"):
        raise RuntimeError(f"/release orders-service starts on {value}: that demo must come down first")
    print("  /release orders-service starts on Jenkins")


def the_package(reset, now=None, stage=None):
    """Read PyPI now. Nothing young enough: this step fails, and nothing is staged. Otherwise 1.32.0 with today's pins."""
    if reset.dry_run:
        if pick.show(now) != 0:
            raise RuntimeError("nothing on the watch list qualifies right now, so a real run would stage nothing")
        return
    reset.act(f"stage {SERVICE} {STAGED} on main with today's pins", lambda: (stage or pick.stage)(STAGED))


def run(dry_run, slack_url):
    started = time.monotonic()
    shared = shared_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset video 30: {SERVICE} {BASELINE} in staging and production, {STAGED} on main with today's pins"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Release runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step("3. The takes' tags and GitHub releases", lambda: shared.github_takes(reset))
    reset.step("4. The takes' release copies in JFrog", lambda: shared.jfrog_takes(reset))
    reset.step(f"5. Jira: {NAME} and the versions", lambda: jira_step(reset, shared))
    reset.step("6. The other demos' switches", lambda: shared.switches_off(reset))
    reset.step("7. /release on Jenkins", on_jenkins)
    reset.step(f"8. The package, and {STAGED} on main", lambda: the_package(reset))
    reset.step("9. Lambda aliases, and one call each", lambda: shared.lambda_aliases(reset))
    reset.step("10. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))
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
        return host.hand_over("reset", "demos/young_dependency/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py young-dependency", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
