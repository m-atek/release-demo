"""Put demo 15 back where a take starts: the main demo's starting state, then 1.5.0 staged.

    python scripts/reset.py flaky                 from the Mac (hands over to the tools host) or the reset job
    python scripts/reset.py flaky --dry-run       every action it would take, and nothing else

The main reset's steps, as reset.py does them for the main demo (stop the
take's runs, cancel open changes, archive the take's tags, GitHub releases
and release copies, un-release the Jira versions after 1.3.0, both aliases on
1.3.0 and called once, the Slack handler warmed), with ORD-[r] reopened too.
Then:

- the quarantine entries the takes made end, with one commit to main as the
  GitHub App, "[skip ci]" (git history keeps them), and their Jira issues move
  to Done with the comment "Earlier take";
- 1.5.0 is staged on main, in place of 1.3.0.

The night's per-test results stay: Grafana keeps them 14 days. Safe to run
twice, under two minutes.
"""

import argparse
import base64
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(1, str(SCRIPTS))

import host  # noqa: E402
import quarantine  # noqa: E402
from common import github_api, github_app_token, service, ssm_client  # noqa: E402

SERVICE = "orders-service"
STATE = "1.5.0"
TAKE_COMMENT = "Earlier take"


def shared_reset():
    """scripts/reset.py as a module: this file has its name."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def story_key():
    """ORD-[r], which setup.py made: found by its summary."""
    from jira import PROJECT, Jira
    from setup import SUMMARY

    found = Jira().search(f'project = {PROJECT} AND summary ~ "\\"{SUMMARY}\\""', fields="summary")
    keys = [issue["key"] for issue in found if issue["fields"]["summary"] == SUMMARY]
    if not keys:
        raise RuntimeError(f'No Jira story "{SUMMARY}" yet: run scripts/demos/flaky/setup.py once.')
    return keys[0]


def end_quarantine(reset):
    """Remove the takes' entries from the quarantine file on main, and close their issues."""
    from jira import Jira, paragraph

    file = service(SERVICE)["tests"]["quarantine"]["file"]
    token, repository, _ = github_app_token(ssm_client())
    path = f"/repos/{repository}/contents/{file}"
    current = github_api("GET", f"{path}?ref=main", token, scheme="token")
    entries = quarantine.load_text(base64.b64decode(current["content"]).decode())
    ending = [entry for entry in entries if entry["service"] == SERVICE]
    if not ending:
        print(f"  {file}: no {SERVICE} entries")
        return
    keep = [entry for entry in entries if entry["service"] != SERVICE]
    issues = sorted({entry["issue"] for entry in ending})
    message = f"demo: end the quarantine entries of earlier takes ({', '.join(issues)}) [skip ci]"
    body = {"message": message, "content": base64.b64encode(quarantine.render(keep).encode()).decode(),
            "sha": current["sha"], "branch": "main"}
    reset.act(f"remove {len(ending)} entr{'ies' if len(ending) > 1 else 'y'} from {file} on main ({', '.join(issues)})",
              lambda: github_api("PUT", path, token, body, scheme="token"))
    jira = Jira()
    for issue in issues:
        def close(issue=issue):
            jira.call("POST", f"/rest/api/3/issue/{issue}/comment", {"body": paragraph(TAKE_COMMENT)})
            jira.transition(issue, "Done")

        reset.act(f"move {issue} to Done with the comment \"{TAKE_COMMENT}\"", close)


def stage_state(reset):
    current = host.main_version(service(SERVICE)["path"])
    if current == STATE:
        print(f"  main has {SERVICE} {STATE}")
        return
    reset.act(f"stage {SERVICE} {STATE} on main (main has {current})", lambda: host.stage(SERVICE, STATE))


def run(dry_run, slack_url):
    started = time.monotonic()
    shared = shared_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset demo 15 to {SERVICE} {shared.BASELINE} in production and {STATE} on main, keeping the past"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"3. The take's tags and GitHub releases, after {shared.BASELINE}", lambda: shared.github_takes(reset))
    reset.step(f"4. The take's release copies in JFrog, after {shared.BASELINE}", lambda: shared.jfrog_takes(reset))

    def jira():
        shared.DEMO_ISSUES = [*shared.DEMO_ISSUES, story_key()]
        shared.jira_versions(reset)

    reset.step(f"5. Jira versions after {shared.BASELINE}, ORD-12, ORD-13 and the story of 1.5.0", jira)
    reset.step("6. The takes' quarantine entries and their issues", lambda: end_quarantine(reset))
    reset.step(f"7. {SERVICE} {STATE} on main", lambda: stage_state(reset))
    reset.step("8. Lambda aliases, and one call each", lambda: shared.lambda_aliases(reset))
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
        shared = shared_reset()
        url = args.slack_handler_url or shared.slack_handler_url()
        shown = [*(["--dry-run"] if args.dry_run else [])]
        return host.hand_over("reset", "demos/flaky/reset.py", [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py flaky", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
