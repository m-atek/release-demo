"""Put demo 22 back where a take starts: no release run left going, the main
reset, and 1.4.0 staged on main. Safe to run twice, under two minutes.

    python scripts/reset.py azure-devops               or the Jenkins job reset with DEMO=azure-devops
    python scripts/reset.py azure-devops --dry-run     every action it would take, and nothing else

1. A release run still in progress is cancelled as Release Bot; its Stopped
   stage ends the Slack message with "Cancelled after ..." and cancels its change.
2. The main reset, scripts/reset.py: production and main on 1.3.0, the take's
   tags, GitHub releases and JFrog copies archived, ORD-12 reopened, open
   changes cancelled, as reset.py does them.
3. 1.4.0 staged on main with stage.py, from a fresh clone, as the GitHub App.

Release Bot signs in from the tools host's role, so from the Mac this hands
itself over to the tools host through SSM Run Command, as reset.py does.
"""

import argparse
import subprocess
import sys
import time

import release_bot
from release_bot import PROJECT, ROOT

import host  # noqa: E402  (scripts/, which release_bot puts on the path)


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


def cancel_runs(steps):
    values = release_bot.settings()
    bearer = release_bot.token(values)
    runs = release_bot.runs_in_progress(values, bearer)
    if not runs:
        print("  no release runs in progress")
    for run in runs:
        steps.act(f"cancel run {run['id']} ({run.get('name')}), as Release Bot",
                  lambda run=run: release_bot.call("PATCH", f"/{PROJECT}/_apis/build/builds/{run['id']}?api-version=7.1",
                                                   {"status": "cancelling"}, values, bearer))
    if steps.dry_run or not runs:
        return
    # Its Stopped stage ends Slack's message and cancels its change: let it finish first.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and release_bot.runs_in_progress(values, bearer):
        time.sleep(5)


def main_reset(steps):
    args = [sys.executable, str(ROOT / "scripts" / "reset.py"), "main", "--here", *(["--dry-run"] if steps.dry_run else [])]
    if subprocess.run(args, cwd=ROOT).returncode != 0:
        raise RuntimeError("the main reset didn't finish: its lines above say why")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        return host.hand_over("reset-azure-devops", "demos/azure_devops/reset.py", ["--dry-run"] if args.dry_run else [],
                              title="reset.py azure-devops")
    sys.stdout.reconfigure(line_buffering=True)
    started = time.monotonic()
    steps = Steps(args.dry_run)
    print(f"Reset demo 22: orders-service 1.3.0 live and 1.4.0 on main{' (dry run: nothing changes)' if args.dry_run else ''}.")
    steps.step("1. Release runs on Azure Pipelines", lambda: cancel_runs(steps))
    steps.step("2. The main reset", lambda: main_reset(steps))
    steps.step("3. orders-service 1.4.0 on main",
               lambda: steps.act("stage orders-service 1.4.0 on main, as the GitHub App",
                                 lambda: host.stage("orders-service", "1.4.0")))
    took = time.monotonic() - started
    if steps.failed:
        print(f"Not everything reset after {took:.0f}s: {', '.join(steps.failed)}. The messages above say why.")
        return 1
    print(f"Demo 22 is reset, in {took:.0f}s: {steps.actions} action(s)." if not args.dry_run else
          f"Dry run in {took:.0f}s: {steps.actions} action(s) to take.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
