"""Put demo 14 back where a take starts: no release run left waiting, the
main reset, and a fresh pull request. Safe to run twice, under two minutes.

    python scripts/reset.py github-actions               or the Jenkins job reset with DEMO=github-actions
    python scripts/reset.py github-actions --dry-run     every action it would take, and nothing else

1. A release run still waiting for production is rejected as the GitHub App,
   with the comment "Cancelled by reset.py before the next take."; its Stopped
   job then finishes its Slack message. A run still going elsewhere is
   cancelled with your gh login, from the Mac; the tools host has none, so
   there it is named for you to cancel.
2. The main reset, scripts/reset.py: production and main on 1.3.0, the take's
   tags, GitHub releases and JFrog copies archived, ORD-12 reopened, open
   changes cancelled, as reset.py does them.
3. The demo's pull request: an open one is closed (merged ones stay), and a
   fresh one opened from main with your gh login, so it is yours on GitHub.
   On the tools host, with no gh login, this step says so and opens none.
"""

import argparse
import subprocess
import sys
import time

import pr
from github_demo import CANCELLED, ROOT, App, gh, gh_signed_in, release_runs


class Steps:
    def __init__(self, dry_run):
        self.dry_run, self.actions, self.failed = dry_run, 0, []

    def act(self, what, do):
        self.actions += 1
        if self.dry_run:
            print(f"  would {what}")
            return
        do()
        print(f"  {what}: done")

    def step(self, title, body):
        print(title)
        try:
            body()
        except SystemExit as error:  # fail() from a shared script: its message is on stderr
            self.failed.append(title)
            print(f"  failed: {error.code if isinstance(error.code, str) else 'see the message above'}")
        except Exception as error:
            self.failed.append(title)
            print(f"  failed: {error.__class__.__name__}: {error}")


def stop_runs(steps, app):
    runs = release_runs(app)
    if not runs:
        print("  no release runs going")
    for run in runs:
        if run["status"] == "waiting":
            steps.act(f"reject production for run {run['id']} ({run['display_title'][:60]}), as the GitHub App",
                      lambda run=run: app.status("POST", f"/repos/{app.repository}/actions/runs/{run['id']}/deployment_protection_rule",
                                                 {"environment_name": "production", "state": "rejected", "comment": CANCELLED}))
        elif gh_signed_in():
            steps.act(f"cancel run {run['id']} ({run['status']}), with your gh login",
                      lambda run=run: gh("run", "cancel", str(run["id"]), "-R", app.repository))
        else:
            print(f"  run {run['id']} is {run['status']}: cancel it on GitHub ({run['html_url']}); "
                  "the tools host has no gh login")


def main_reset(steps):
    args = [sys.executable, str(ROOT / "scripts" / "reset.py"), "main", *(["--dry-run"] if steps.dry_run else [])]
    if subprocess.run(args, cwd=ROOT).returncode != 0:
        raise RuntimeError("the main reset didn't finish: its lines above say why")


def pull_request(steps, app):
    for pull in pr.open_pulls(app.repository) if gh_signed_in() else []:
        steps.act(f"close #{pull['number']} ({pull['head']['ref']})",
                  lambda pull=pull: pr.gh_api("PATCH", f"/repos/{app.repository}/pulls/{pull['number']}", {"state": "closed"}))
    if not gh_signed_in():
        print("  skipped here: gh isn't signed in. From the Mac: scripts/demo.sh open-pr github-actions")
        return
    steps.act("open a fresh pull request from main, as you", lambda: pr.open_pull(app.repository))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    args = parser.parse_args()

    started = time.monotonic()
    steps = Steps(args.dry_run)
    print(f"Reset demo 14 to orders-service 1.3.0 and a fresh pull request{' (dry run: nothing changes)' if args.dry_run else ''}.")
    app = App()
    steps.step("1. Release runs on GitHub Actions", lambda: stop_runs(steps, app))
    steps.step("2. The main reset", lambda: main_reset(steps))
    steps.step("3. The demo's pull request", lambda: pull_request(steps, app))
    took = time.monotonic() - started
    if steps.failed:
        print(f"Not everything reset after {took:.0f}s: {', '.join(steps.failed)}. The messages above say why.")
        return 1
    print(f"Demo 14 is reset, in {took:.0f}s: {steps.actions} action(s)." if not args.dry_run else
          f"Dry run in {took:.0f}s: {steps.actions} action(s) to take.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
