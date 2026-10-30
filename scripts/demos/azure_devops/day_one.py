"""Day one: prove the check, the bypass and Release Bot's token before anything
else of demo 22 is trusted (its acceptance check 1).

    python scripts/demos/azure_devops/day_one.py token          Release Bot signs in from the tools host, with no secret
    python scripts/demos/azure_devops/day_one.py run            the throwaway pipeline, its test change, and the check
    python scripts/demos/azure_devops/day_one.py clean --yes    delete the throwaway pipeline and environment

Before: setup.py --apply --bridge-test, which makes the environment
bridge-test with production's check and the pipeline bridge-test from
scripts/demos/azure_devops/bridge-test.yml, and demo.sh up azure-devops.

run queues the throwaway pipeline with your az login and opens a test change
whose correlation id names the run, requested by Sam. The run page should
show "ServiceNow change [number] is waiting for approval." as the check's
log. Approve the change as Alex: the stage should start within 20 seconds,
and this prints how many. For the bypass, run it again and, as the owner,
Bypass check: the stage starts, its first step waits for ServiceNow, and the
checks panel names you. The free parallel job: setup.py --check prints it.
Each test change is cancelled at the end. wait-in-run is that first step,
inside the throwaway run on the agent.
"""

import argparse
import calendar
import os
import sys
import time

import release_bot
from release_bot import PROJECT, fail, get_parameter, ssm_client

import host  # noqa: E402  (scripts/, which release_bot puts on the path)

NOTE = "Approval bridge check by day_one.py: cancelled after the check."


def correlation(organization, run_id):
    return f"azure-pipelines:{organization}/{PROJECT}:{run_id}"


def token():
    code, out, err = host.Host().shell(
        f"cd {host.REPO_DIR} && (git pull --ff-only -q || true) && "
        f"/opt/release-demo-venv/bin/python scripts/demos/azure_devops/release_bot.py whoami", timeout=60)
    print("\n".join(part.strip() for part in (out, err) if part and part.strip()))  # whoami's refusal is on stderr
    return code


def utc(text):
    text = text.replace("T", " ").split(".")[0].rstrip("Z")
    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def run():
    from change import connect
    from setup import DevOps

    organization = get_parameter(ssm_client(), "azure-devops/organization")
    devops = DevOps(organization)
    pipeline = next((p for p in devops.call("GET", f"/{PROJECT}/_apis/pipelines?api-version=7.1")["value"]
                     if p["name"] == "bridge-test"), None)
    if pipeline is None:
        fail("No pipeline bridge-test: setup.py --apply --bridge-test first.")
    queued = devops.call("POST", f"/{PROJECT}/_apis/pipelines/{pipeline['id']}/runs?api-version=7.1",
                         {"resources": {"repositories": {"self": {"refName": "refs/heads/main"}}}})
    run_id = queued["id"]
    sn, _ = connect()
    ci = sn.find("cmdb_ci_appl", "name=orders-service", "sys_id")["sys_id"]
    sam = sn.find("sys_user", "user_name=sam.developer", "sys_id")["sys_id"]
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")["sys_id"]
    reply = sn.call("POST", "/api/sn_chg_rest/change/normal", {
        "short_description": "Approval bridge check by day_one.py: cancelled after the check", "cmdb_ci": ci,
        "assignment_group": group, "requested_by": sam, "correlation_id": correlation(organization, run_id)})
    change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
    sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": "-4"})
    print(f"Run {run_id}: {queued['_links']['web']['href']}")
    print(f"Test change {number}. The check's log should say: \"ServiceNow change {number} is waiting for approval.\"")
    print(f"Approve {number} as Alex, reject it, or bypass the check as the owner. Watching (up to 10 minutes)...")
    try:
        decided, deadline = None, time.monotonic() + 600
        while time.monotonic() < deadline:
            record = sn.call("GET", f"/api/now/table/change_request/{change_id}", sysparm_fields="approval,sys_updated_on")
            if not decided and record["approval"] in ("approved", "rejected"):
                decided = (record["approval"], utc(record["sys_updated_on"]))
                print(f"{number}: {decided[0]} in ServiceNow")
            timeline = devops.call("GET", f"/{PROJECT}/_apis/build/builds/{run_id}/timeline?api-version=7.1")
            job = next((r for r in timeline.get("records", []) if r["type"] == "Job" and r.get("startTime")), None)
            if decided and decided[0] == "approved" and job:
                took = utc(job["startTime"]) - decided[1]
                print(f"The stage started {took} s after the approval "
                      f"({'ok' if took < 20 else 'TOO SLOW: fix the bridge before recording'}).")
                return 0 if took < 20 else 1
            if decided and decided[0] == "rejected" and timeline.get("records") and any(
                    r.get("result") == "failed" for r in timeline["records"]):
                print("The check failed with the bridge's line: see the run page.")
                return 0
            time.sleep(2)
        print("No answer within 10 minutes.")
        return 1
    finally:
        sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": "4", "close_notes": NOTE, "work_notes": NOTE})
        print(f"{number}: cancelled")


def wait_in_run():
    """The throwaway run's first step, on the agent: change.py wait for its test change."""
    import change
    import run_state

    organization = get_parameter(ssm_client(), "azure-devops/organization")
    sn, _ = change.connect()
    found = sn.find("change_request", f"correlation_id={correlation(organization, os.environ['BUILD_BUILDID'])}",
                    "sys_id,number,requested_by")
    if not found:
        fail("No test change for this run.")
    state = {"change": {"sys_id": found["sys_id"], "number": found["number"], "type": "normal",
                        "requested_by": found["requested_by"]["value"] if isinstance(found["requested_by"], dict)
                        else found["requested_by"]}}
    run_state.save(state)
    print(f"Waiting for ServiceNow change {found['number']}.")
    change.wait(sn, state, 1800, 15)
    return 0


def clean():
    from setup import DevOps

    devops = DevOps(get_parameter(ssm_client(), "azure-devops/organization"))
    for pipeline in devops.call("GET", f"/{PROJECT}/_apis/pipelines?api-version=7.1")["value"]:
        if pipeline["name"] == "bridge-test":
            devops.call("DELETE", f"/{PROJECT}/_apis/build/definitions/{pipeline['id']}?api-version=7.1")
            print("Deleted the pipeline bridge-test")
    for environment in devops.call("GET", f"/{PROJECT}/_apis/pipelines/environments?name=bridge-test&api-version=7.1")["value"]:
        devops.call("DELETE", f"/{PROJECT}/_apis/pipelines/environments/{environment['id']}?api-version=7.1")
        print("Deleted the environment bridge-test")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["token", "run", "wait-in-run", "clean"])
    parser.add_argument("--yes", action="store_true", help="for clean: Mateusz has said yes to deleting both")
    args = parser.parse_args()
    if args.action == "clean" and not args.yes:
        fail("clean deletes the pipeline and the environment bridge-test. Ask Mateusz, then add --yes.")
    return {"token": token, "run": run, "wait-in-run": wait_in_run, "clean": clean}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
