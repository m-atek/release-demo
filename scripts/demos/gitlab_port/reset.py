"""Put demo 17 back where a take starts: no GitLab pipeline left going,
production and main on 1.3.0, then 1.4.0 staged on main and on GitLab's
copy. Safe to run twice, under two minutes. From the Mac.

    python scripts/reset.py gitlab-port               cancel, main reset, stage 1.4.0, sync
    python scripts/reset.py gitlab-port --dry-run     every action it would take, and nothing else

1. A GitLab pipeline still going is cancelled; its running job's after_script
   ends the Slack message with "Cancelled after ..." and cancels its change.
2. The main reset, scripts/reset.py: production and main on 1.3.0, the take's
   tags, GitHub releases and JFrog copies archived, ORD-12 reopened, open
   changes cancelled, as reset.py does them.
3. 1.4.0 staged on main with stage.py, from a fresh clone, as the GitHub App.
4. GitLab's main synced to GitHub's (sync.py).

The tags before-gitlab and gitlab-port stay: they aren't version tags. The
tools host may not read gitlab/api-token, so the Jenkins job reset can't run
this demo's reset; it says so in one line.
"""

import argparse
import subprocess
import sys
import time

import sync
from gitlab_demo import ROOT, GitLab, host

GOING = ("created", "waiting_for_resource", "preparing", "pending", "running", "scheduled")


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


def going(gl, project):
    found = []
    for status in GOING:
        found += gl.call("GET", f"/projects/{project['id']}/pipelines?status={status}&per_page=20") or []
    return sorted({p["id"]: p for p in found}.values(), key=lambda p: p["id"])


def cancel_pipelines(steps, gl, project):
    pipelines = going(gl, project)
    if not pipelines:
        print("  no GitLab pipelines going")
    for pipeline in pipelines:
        steps.act(f"cancel pipeline {pipeline['id']} ({pipeline['status']})",
                  lambda pipeline=pipeline: gl.call("POST", f"/projects/{project['id']}/pipelines/{pipeline['id']}/cancel"))
    if steps.dry_run or not pipelines:
        return
    # The running job's after_script ends Slack's message and cancels its change: let it finish first.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and going(gl, project):
        time.sleep(3)


def main_reset(steps):
    args = [sys.executable, str(ROOT / "scripts" / "reset.py"), "main", *(["--dry-run"] if steps.dry_run else [])]
    if subprocess.run(args, cwd=ROOT).returncode != 0:
        raise RuntimeError("the main reset didn't finish: its lines above say why")


def stage(steps):
    steps.act("stage orders-service 1.4.0 on main, as the GitHub App", lambda: host.stage("orders-service", "1.4.0"))


def sync_main(steps, gl):
    if steps.dry_run:
        sync.sync(gl, check_only=True)
        print("  would push GitHub's main to GitLab's")
        return
    if sync.sync(gl) != 0:
        raise RuntimeError("GitLab's main isn't synced")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    args = parser.parse_args()

    started = time.monotonic()
    gl = GitLab()  # stops at once on the tools host, which may not read the account's token
    project = gl.project()
    steps = Steps(args.dry_run)
    print(f"Reset demo 17: orders-service 1.3.0 live, 1.4.0 on main and on GitLab{' (dry run: nothing changes)' if args.dry_run else ''}.")
    steps.step("1. GitLab pipelines", lambda: cancel_pipelines(steps, gl, project))
    steps.step("2. The main reset", lambda: main_reset(steps))
    steps.step("3. orders-service 1.4.0 on main", lambda: stage(steps))
    steps.step("4. GitLab's main", lambda: sync_main(steps, gl))
    took = time.monotonic() - started
    if steps.failed:
        print(f"Not everything reset after {took:.0f}s: {', '.join(steps.failed)}. The messages above say why.")
        return 1
    print(f"Demo 17 is reset, in {took:.0f}s: {steps.actions} action(s)." if not args.dry_run else
          f"Dry run in {took:.0f}s: {steps.actions} action(s) to take.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
