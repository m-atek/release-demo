"""Day one: prove the runner and the trigger on the real project, before
anything else of demo 17 is trusted (its acceptance check 1). From the Mac.

    python scripts/demos/gitlab_port/day_one.py run           a throwaway branch's one job, started by the trigger token
    python scripts/demos/gitlab_port/day_one.py clean --yes   delete the throwaway branch, once Mateusz has said yes

Before: setup.py (the project, instance runners off, the runner and the
trigger token in SSM) and the tools host up. run registers and starts the
runner, syncs GitLab's main, commits a one-job .gitlab-ci.yml to the branch
day-one-check, made from main, starts
it with the trigger token, and waits a minute for the job to run on
tools-host. If GitLab holds the pipeline for identity verification, or the
job runs anywhere else, it says so: verifying with a phone or a card is
Mateusz's decision. It stops the runner again at the end.
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

import sync
from gitlab_demo import API, RUNNER, USER_AGENT, GitLab, GitLabError, fail, get_parameter, install_runner, runner_unit

BRANCH = "day-one-check"
PIPELINE = """# Day one only, then deleted: does a trigger start a job on the project runner?
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "trigger"

check:
  tags: [tools-host]
  script:
    - echo "Ran on $(hostname) as $(id -un)."
"""


def branch(gl, project):
    try:
        gl.call("GET", f"/projects/{project['id']}/repository/branches/{BRANCH}")
        return
    except GitLabError as error:
        if error.status != 404:
            raise
    # From main, which sync.py pushed first, so the project's default branch is main, not this one.
    gl.call("POST", f"/projects/{project['id']}/repository/commits", {
        "branch": BRANCH, "start_branch": "main", "commit_message": "Day one: a throwaway pipeline for the runner check",
        "actions": [{"action": "update", "file_path": ".gitlab-ci.yml", "content": PIPELINE}]})
    print(f"Branch {BRANCH}: created from main, with a one-job .gitlab-ci.yml")


def trigger(gl, project):
    token = get_parameter(gl.ssm, "gitlab/trigger-token")
    request = urllib.request.Request(f"{API}/projects/{project['id']}/trigger/pipeline", method="POST",
                                     data=urllib.parse.urlencode({"token": token, "ref": BRANCH}).encode(),
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def run():
    gl = GitLab()
    project = gl.project()
    install_runner()
    print(f"runner on the tools host: {runner_unit('start')}")
    try:
        if sync.sync(gl) != 0:
            fail("GitLab's main isn't synced.")
        branch(gl, project)
        pipeline = trigger(gl, project)
        print(f"Pipeline {pipeline['id']}: {pipeline['web_url']}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            jobs = gl.call("GET", f"/projects/{project['id']}/pipelines/{pipeline['id']}/jobs")
            job = jobs[0] if jobs else {}
            status = job.get("status")
            if status == "success":
                runner = (job.get("runner") or {}).get("description")
                print(f"The job ran on {runner}." + ("" if runner == RUNNER else f" Not {RUNNER}: stop and tell Mateusz."))
                return 0 if runner == RUNNER else 1
            if status in ("failed", "canceled"):
                print(f"The job {status}: {job.get('failure_reason')}. If GitLab asks for identity verification, "
                      "stop and tell Mateusz: verifying with a phone or a card is his decision.")
                return 1
            time.sleep(3)
        print("The job didn't run on tools-host within a minute. Look at the pipeline: GitLab may be holding it "
              "for identity verification. Stop and tell Mateusz.")
        return 1
    finally:
        print(f"runner on the tools host: {runner_unit('stop')}")


def clean():
    gl = GitLab()
    project = gl.project()
    gl.call("DELETE", f"/projects/{project['id']}/repository/branches/{BRANCH}", ok=(204, 404))
    print(f"Branch {BRANCH}: deleted")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["run", "clean"])
    parser.add_argument("--yes", action="store_true", help="for clean: Mateusz has said yes to deleting the branch")
    args = parser.parse_args()
    if args.action == "clean":
        if not args.yes:
            fail(f"clean deletes the branch {BRANCH} on GitLab. Ask Mateusz, then add --yes.")
        return clean()
    return run()


if __name__ == "__main__":
    sys.exit(main())
