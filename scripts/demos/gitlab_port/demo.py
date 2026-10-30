"""scripts/demo.sh up, down and status for demo 17, GitLab CI. From the Mac.

    python scripts/demos/gitlab_port/demo.py up        the runner on, GitLab's main synced, then /release on GitLab
    python scripts/demos/gitlab_port/demo.py down      /release back on Jenkins, then the runner off
    python scripts/demos/gitlab_port/demo.py status    the switch, the runner, and both mains

up runs tools-host/gitlab-runner.sh on the tools host (it installs and
registers the runner once, as boot.sh does), starts the runner, waits until
GitLab shows it online, syncs GitLab's main, and only then sets
ci/orders-service to gitlab-ci. down sets it back to jenkins first, so no
/release can reach a runner that is stopping, then stops the runner. The
tools host must be running: scripts/tools.sh up.
"""

import argparse
import sys
import time

import sync
from gitlab_demo import RUNNER, GitLab, fail, install_runner, runner_unit, switch


def online(gl, project):
    return [r for r in gl.runners(project["id"]) if r.get("description") == RUNNER and r.get("status") == "online"]


def up():
    gl = GitLab()
    project = gl.project()
    install_runner()
    print(f"runner on the tools host: {runner_unit('start')}")
    deadline = time.monotonic() + 120
    while not online(gl, project):
        if time.monotonic() > deadline:
            fail(f"GitLab doesn't show {RUNNER} online after two minutes. See: journalctl -u release-demo-gitlab-runner on the host.")
        time.sleep(5)
    print(f"GitLab: {RUNNER} online")
    if sync.sync(gl) != 0:
        fail("GitLab's main isn't synced: /release stays on Jenkins.")
    print(f"/release orders-service now runs on: {switch(gl.ssm, 'gitlab-ci')}")
    print("Ready")
    return 0


def down():
    gl = GitLab()
    print(f"/release orders-service now runs on: {switch(gl.ssm, 'jenkins')}")
    print(f"runner on the tools host: {runner_unit('stop')}")
    return 0


def status():
    gl = GitLab()
    project = gl.project()
    print(f"/release orders-service runs on: {switch(gl.ssm)}")
    try:
        print(f"runner on the tools host: {runner_unit('is-active')}")
    except SystemExit:
        print("runner on the tools host: the tools host is stopped")
    for runner in gl.runners(project["id"]):
        print(f"runner on GitLab: {runner['description']} {runner.get('status')}")
    sync.sync(gl, check_only=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    args = parser.parse_args()
    return {"up": up, "down": down, "status": status}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
