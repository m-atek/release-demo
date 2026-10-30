"""Day one: prove the approval bridge on a throwaway environment and workflow,
before anything else of demo 14 is trusted (its acceptance check 1).

    python scripts/demos/github_actions/bridge_test.py run          one run: you approve or reject its test change as Alex
    python scripts/demos/github_actions/bridge_test.py clean --yes  remove the throwaway workflow and environment

Before: environments.py --apply --bridge-test (the environment bridge-test,
with the GitHub App as its rule), and the app's webhook (app_hook.py).

run puts .github/workflows/bridge-test.yml on main as the GitHub App, if it
isn't there, starts it with your gh login, and opens a test change in
ServiceNow whose correlation id names the run, requested by Sam. Its one job
waits on bridge-test, and the run page shows the bridge's report naming the
change. Approve the change as Alex: the job should start within 20 seconds,
and this prints how many. Reject another: the job fails with the bridge's
comment. Each test change is cancelled at the end. clean deletes the
workflow file and the environment; run it only once Mateusz has said yes.
"""

import argparse
import base64
import calendar
import json
import sys
import time

from github_demo import ROOT, App, fail, gh, gh_api, gh_signed_in, repository

from change import connect  # noqa: E402  (scripts/, which github_demo puts on the path)

WORKFLOW = "bridge-test.yml"
SOURCE = "scripts/demos/github_actions/bridge-test.yml"
NOTE = "Approval bridge check by bridge_test.py: cancelled after the check."


def workflow_on_main(app):
    status, _ = app.status("GET", f"/repos/{app.repository}/contents/.github/workflows/{WORKFLOW}?ref=main")
    if status == 200:
        return
    content = base64.b64encode((ROOT / SOURCE).read_bytes()).decode()
    app.call("PUT", f"/repos/{app.repository}/contents/.github/workflows/{WORKFLOW}",
             {"message": "Day one: a throwaway workflow for the approval bridge [skip ci]", "content": content,
              "branch": "main"})
    print(f"Put .github/workflows/{WORKFLOW} on main")


def start_run(repo):
    before = {r["databaseId"] for r in runs(repo)}
    gh("workflow", "run", WORKFLOW, "-R", repo, "--ref", "main")
    for _ in range(30):
        time.sleep(2)
        new = [r for r in runs(repo) if r["databaseId"] not in before]
        if new:
            return str(new[0]["databaseId"])
    fail("GitHub didn't start the throwaway workflow within a minute.")


def runs(repo):
    out = gh("run", "list", "-R", repo, "--workflow", WORKFLOW, "--json", "databaseId", "--limit", "10").stdout
    return json.loads(out or "[]")


def test_change(sn, repo, run_id):
    ci = sn.find("cmdb_ci_appl", "name=orders-service", "sys_id")["sys_id"]
    sam = sn.find("sys_user", "user_name=sam.developer", "sys_id")["sys_id"]
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")["sys_id"]
    reply = sn.call("POST", "/api/sn_chg_rest/change/normal", {
        "short_description": "Approval bridge check by bridge_test.py: cancelled after the check",
        "cmdb_ci": ci, "assignment_group": group, "requested_by": sam,
        "correlation_id": f"github-actions:{repo}:{run_id}"})
    change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
    sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": "-4"})
    return change_id, number


def utc(text):
    """ServiceNow's and GitHub's UTC times to epoch seconds."""
    text = text.replace("T", " ").rstrip("Z")
    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def watch(sn, app, run_id, change_id, number):
    print(f"Approve {number} as Alex now, or reject it. Watching (up to 10 minutes)...")
    decided = None
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        record = sn.call("GET", f"/api/now/table/change_request/{change_id}", sysparm_fields="approval,sys_updated_on")
        if not decided and record["approval"] in ("approved", "rejected"):
            decided = (record["approval"], utc(record["sys_updated_on"]))
            print(f"{number}: {decided[0]} in ServiceNow")
        jobs = app.call("GET", f"/repos/{app.repository}/actions/runs/{run_id}/jobs")["jobs"]
        job = jobs[0] if jobs else {}
        if decided and decided[0] == "approved" and job.get("started_at") and job.get("status") != "waiting":
            took = utc(job["started_at"]) - decided[1]
            verdict = "ok" if took < 20 else "TOO SLOW: fix the bridge before recording"
            print(f"The job started {took} s after the approval ({verdict}).")
            return 0 if took < 20 else 1
        if decided and decided[0] == "rejected" and job.get("conclusion") == "failure":
            print("The job failed with the bridge's comment: see the run page.")
            return 0
        time.sleep(1)
    print("No answer within 10 minutes.")
    return 1


def run():
    if not gh_signed_in():
        fail("gh isn't signed in: gh auth login.")
    app = App()
    workflow_on_main(app)
    run_id = start_run(app.repository)
    sn, _ = connect()
    change_id, number = test_change(sn, app.repository, run_id)
    print(f"Run {run_id}: https://github.com/{app.repository}/actions/runs/{run_id}")
    print(f"Test change {number}. The run should show: \"ServiceNow change {number} is waiting for approval.\"")
    try:
        return watch(sn, app, run_id, change_id, number)
    finally:
        sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": "4", "close_notes": NOTE, "work_notes": NOTE})
        print(f"{number}: cancelled")


def clean():
    if not gh_signed_in():
        fail("gh isn't signed in: gh auth login.")
    app, repo = App(), repository()
    status, reply = app.status("GET", f"/repos/{repo}/contents/.github/workflows/{WORKFLOW}?ref=main")
    if status == 200:
        app.call("DELETE", f"/repos/{repo}/contents/.github/workflows/{WORKFLOW}",
                 {"message": "Day one done: the approval bridge's throwaway workflow goes [skip ci]", "sha": reply["sha"],
                  "branch": "main"})
        print(f"Deleted .github/workflows/{WORKFLOW}")
    if gh_api("GET", f"/repos/{repo}/environments/bridge-test", check=False):
        gh_api("DELETE", f"/repos/{repo}/environments/bridge-test")
        print("Deleted the environment bridge-test")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["run", "clean"])
    parser.add_argument("--yes", action="store_true", help="for clean: Mateusz has said yes to deleting both")
    args = parser.parse_args()
    if args.action == "clean":
        if not args.yes:
            fail("clean deletes the throwaway workflow and the environment bridge-test. Ask Mateusz, then add --yes.")
        return clean()
    return run()


if __name__ == "__main__":
    sys.exit(main())
