"""Stages 8 to 12: the ServiceNow change that records the release.

    python scripts/change.py open                  a normal change, moved to Assess; with option standard, a pre-approved one
    python scripts/change.py note "Staging: ok"    a work note, once the change exists
    python scripts/change.py wait                  poll the approval every 15 seconds, up to 30 minutes
    python scripts/change.py implement
    python scripts/change.py close successful|unsuccessful --notes "..."
    python scripts/change.py cancel --notes "..."
    python scripts/change.py rollback successful|unsuccessful --notes "..."   a /rollback, from the rollback template

Works through ServiceNow's Change Management API as release.pipeline. The
change names the service's CMDB item, who asked (Sam), the plans, the
evidence as attachments, and the links. An approval by the person who asked
is refused, whatever ServiceNow says.
"""

import argparse
import mimetypes
import sys
import time
import urllib.parse
import urllib.request

import run_state
from common import ROOT, fail, get_parameter, people, service, ssm_client
from servicenow_setup import ServiceNow

ASSESS, SCHEDULED, IMPLEMENT, REVIEW, CLOSED, CANCELED = "-4", "-2", "-1", "0", "3", "4"
EVIDENCE = ["junit.xml", "sbom.cdx.json", "grype.json", "build-info.json"]


def connect():
    ssm = ssm_client()
    return ServiceNow(get_parameter(ssm, "servicenow/instance-url"), "release.pipeline",
                      get_parameter(ssm, "servicenow/release-pipeline-password")), ssm


def requester_user(sn, slack_id):
    """Sam's ServiceNow user from his Slack id, through people.yaml."""
    for person in people().values():
        if slack_id and person.get("slack_user_id") == slack_id and person.get("servicenow_user"):
            return sn.find("sys_user", f"user_name={person['servicenow_user']}", "sys_id,first_name")
    return sn.find("sys_user", "user_name=release.pipeline", "sys_id,first_name")


def links(state, ssm):
    jira = get_parameter(ssm, "jira/site-url").rstrip("/")
    org = get_parameter(ssm, "sonar/organization")
    config = service(state["service"])
    change = state.get("change", {}).get("number", "")
    search = urllib.parse.quote(f'search sourcetype="release:event" change="{change}" '
                                '| table _time stage result seconds environment | sort _time')
    return {
        "Jira": f"{jira}/browse/{state['jira_key']}",
        "Jenkins build": state["build"].get("url", ""),
        "Package": f"https://artifactory-demo.es.nl/artifactory/{state['package']['path']} (SHA-256 {state['package']['sha256']})",
        "SonarQube": f"https://sonarcloud.io/dashboard?id={config['sonarqube']['project_key']}&organization={org}",
        "Splunk": f"https://splunk-demo.es.nl/en-US/app/search/search?q={search}",
        "Grafana": get_parameter(ssm, "grafana/stack-url").rstrip("/") + "/d/release-demo",
    }


def stage_summary(state):
    done = []
    for name, stage in state["stages"].items():
        if stage["status"] in ("ok", "failed", "skipped"):
            took = f" in {run_state.duration(stage['seconds'])}" if stage.get("seconds") else ""
            done.append(f"{name}: {stage['status']}{took}")
    return "\n".join(done)


def open_change(sn, ssm, state):
    config = service(state["service"])
    # A service may release as standard changes by default (services.yaml servicenow.change).
    standard = state.get("option") == "standard" or config["servicenow"].get("change") == "standard"
    groups = {g: sn.find("sys_user_group", f"name={g}", "sys_id")["sys_id"] for g in ("Platform team",)}
    ci = sn.find("cmdb_ci_appl", f"name={config['servicenow']['cmdb_item']}", "sys_id")["sys_id"]
    asked_by = requester_user(sn, state.get("requested_by"))
    started = run_state.now()
    fields = {
        "short_description": f"Release {state['service']} {state['version']} ({state['jira_key']})",
        "description": f"Release of {state['service']} {state['version']} for {state['jira_key']}, started from Slack "
                       f"and run by the release pipeline. Demo system, not a client's.",
        "cmdb_ci": ci, "assignment_group": groups["Platform team"], "requested_by": asked_by["sys_id"],
        "category": "Software",
        "justification": f"{state['jira_key']}: {links(state, ssm)['Jira']}",
        "implementation_plan": "The release pipeline runs: build, tests, quality gate, SBOM and scan, publish to "
                               "JFrog Container Registry, staging and its check, this change, production with the "
                               f"same package verified by its SHA-256, and the production check.\nBuild: {state['build'].get('url', '')}",
        "backout_plan": config["servicenow"].get("backout_plan") or (
            "Automatic: if the production check fails, the pipeline moves the live alias back to the "
            "previous version, checks it, and closes this change as unsuccessful with the rollback in its notes."),
        "test_plan": "Before this change:\n" + stage_summary(state),
        "start_date": run_state.iso(started).replace("T", " ").rstrip("Z"),
        "end_date": run_state.iso(started + 1800).replace("T", " ").rstrip("Z"),
    }
    if standard:
        name = config["servicenow"]["templates"]["standard"]
        template = sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id")
        reply = sn.call("POST", f"/api/sn_chg_rest/change/standard/{template['sys_id']}", fields)
    else:
        reply = sn.call("POST", "/api/sn_chg_rest/change/normal", fields)
    change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
    state["change"] = {"sys_id": change_id, "number": number, "type": "standard" if standard else "normal",
                       "url": f"{sn.url}/change_request.do?sys_id={change_id}", "requested_by": asked_by["sys_id"]}
    run_state.save(state)

    for name in EVIDENCE:
        path = run_state.REPORTS / name
        if not path.exists() and name == "build-info.json":
            path = ROOT / "dist" / state["service"] / name
        if path.exists():
            attach(sn, change_id, path)
    note(sn, state, "Evidence:\n" + "\n".join(f"{k}: {v}" for k, v in links(state, ssm).items()))
    note(sn, state, "Stages so far:\n" + stage_summary(state))
    if not standard:
        sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": ASSESS})
    print(f"{number}: {'standard, pre-approved' if standard else 'normal, waiting for Change approvers'}")


def attach(sn, change_id, path):
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    url = (f"{sn.url}/api/now/attachment/file?table_name=change_request&table_sys_id={change_id}"
           f"&file_name={urllib.parse.quote(path.name)}")
    request = urllib.request.Request(url, data=path.read_bytes(), method="POST",
                                     headers={"Authorization": sn.auth, "Content-Type": content_type,
                                              "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60):
        pass


def note(sn, state, text):
    change = state.get("change")
    if change:
        sn.call("PATCH", f"/api/now/table/change_request/{change['sys_id']}", {"work_notes": text})


def wait(sn, state, timeout, interval):
    change = state["change"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = sn.call("GET", f"/api/now/table/change_request/{change['sys_id']}", sysparm_fields="approval,state")
        if record["approval"] == "rejected":
            cancel(sn, state, "Rejected in ServiceNow. The release stopped before production.")
            fail("The change was rejected.")
        approvals = sn.call("GET", "/api/now/table/sysapproval_approver", sysparm_query=f"sysapproval={change['sys_id']}^state=approved",
                            sysparm_fields="approver")
        approvers = [a["approver"]["value"] for a in approvals]
        if change["requested_by"] in approvers:
            cancel(sn, state, "Approved by the person who asked for the release. Refused by the pipeline: "
                              "the person who asks can't approve.")
            fail("The change was approved by the person who asked for it. Refused.")
        if record["approval"] == "approved" and approvers:
            approver = sn.find("sys_user", f"sys_id={approvers[0]}", "first_name,name")
            state["change"]["approved_by"] = approver["first_name"] or approver["name"]
            run_state.save(state)
            print(f"Approved by {state['change']['approved_by']} in ServiceNow.")
            return
        time.sleep(interval)
    cancel(sn, state, f"No approval within {timeout // 60} minutes. The release stopped before production.")
    fail("No approval in time.")


def implement(sn, state, started=None):
    """Into Implement. A standard change starts in New and, like any change,
    may only reach Implement from Scheduled, so it goes through Scheduled first.
    The work starts now, or at started (epoch seconds) for work already done."""
    change = state["change"]
    path = f"/api/sn_chg_rest/change/{change['type']}/{change['sys_id']}"
    current = sn.call("GET", f"/api/now/table/change_request/{change['sys_id']}", sysparm_fields="state")["state"]
    if current not in (SCHEDULED, IMPLEMENT):
        sn.call("PATCH", path, {"state": SCHEDULED})
    started = started or run_state.now()
    sn.call("PATCH", path, {"state": IMPLEMENT, "work_start": run_state.iso(started).replace("T", " ").rstrip("Z")})


def close(sn, state, code, notes):
    change = state["change"]
    path = f"/api/sn_chg_rest/change/{change['type']}/{change['sys_id']}"
    sn.call("PATCH", path, {"state": REVIEW, "work_end": run_state.iso(run_state.now()).replace("T", " ").rstrip("Z")})
    sn.call("PATCH", path, {"state": CLOSED, "close_code": code, "close_notes": notes})


def record_rollback(sn, ssm, state, code, notes):
    """A /rollback, recorded once production is back: a pre-approved change from
    the service's rollback template, in Implement from the rollback's start,
    then closed with its result. It comes after the move, so a sleeping
    ServiceNow never holds up the recovery. The run state is scripts/rollback.py's."""
    config = service(state["service"])
    name = config["servicenow"]["templates"]["rollback"]
    template = sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id")
    if template is None:
        fail(f"ServiceNow has no active standard change template '{name}'. Run scripts/servicenow_setup.py.")
    production = state.get("deploys", {}).get("production", {})
    was, was_number = production.get("previous_version", "?"), production.get("previous_number", "?")
    back, back_number = production.get("rolled_back_to", "?"), production.get("rolled_back_number", "?")
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")["sys_id"]
    ci = sn.find("cmdb_ci_appl", f"name={config['servicenow']['cmdb_item']}", "sys_id")["sys_id"]
    asked_by = requester_user(sn, state.get("requested_by"))
    build = state["build"].get("url", "")
    key = state.get("jira_key")
    issue = f"{key}: {get_parameter(ssm, 'jira/site-url').rstrip('/')}/browse/{key}" if key else "no Jira issue has its fix version"
    fields = {
        "short_description": f"Rollback of {state['service']} in production: {was} to {back}",
        "description": f"/rollback in Slack moved {state['service']} in production from {was} back to {back}, the "
                       "release before it, and checked it. Recorded by the rollback job. Demo system, not a client's.",
        "cmdb_ci": ci, "assignment_group": group, "requested_by": asked_by["sys_id"], "category": "Software",
        "justification": f"Roll back the release of {state['service']} {was}. Its issue: {issue}",
        "implementation_plan": f"The rollback job moved production from version {was_number} ({was}) to version "
                               f"{back_number} ({back}) and checked it.\nBuild: {build}",
        "backout_plan": f"Move production forward again to version {was_number} ({was}).",
        "test_plan": f"/health must report {back}, then every production check path must answer 200.",
        "start_date": run_state.iso(state["started_at"]).replace("T", " ").rstrip("Z"),
        "end_date": run_state.iso(run_state.now()).replace("T", " ").rstrip("Z"),
    }
    reply = sn.call("POST", f"/api/sn_chg_rest/change/standard/{template['sys_id']}", fields)
    change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
    state["change"] = {"sys_id": change_id, "number": number, "type": "standard",
                       "url": f"{sn.url}/change_request.do?sys_id={change_id}", "requested_by": asked_by["sys_id"]}
    run_state.save(state)

    search = urllib.parse.quote(f'search sourcetype="release:event" build_url="{build}"')
    note(sn, state, "Rollback steps:\n" + stage_summary(state) + f"\n\nJenkins build: {build}\n"
                    f"Splunk: https://splunk-demo.es.nl/en-US/app/search/search?q={search}\n"
                    f"Grafana: {get_parameter(ssm, 'grafana/stack-url').rstrip('/')}/d/release-demo")
    implement(sn, state, started=state["started_at"])
    close(sn, state, code, notes)
    print(f"{number}: rollback {was} to {back}, standard, closed {code}")


def cancel(sn, state, notes):
    change = state.get("change")
    if change:
        note(sn, state, notes)
        sn.call("PATCH", f"/api/sn_chg_rest/change/{change['type']}/{change['sys_id']}",
                {"state": CANCELED, "close_notes": notes})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("open")
    noter = sub.add_parser("note")
    noter.add_argument("text")
    waiter = sub.add_parser("wait")
    waiter.add_argument("--timeout", type=int, default=1800)
    waiter.add_argument("--interval", type=int, default=15)
    sub.add_parser("implement")
    closer = sub.add_parser("close")
    closer.add_argument("code", choices=["successful", "unsuccessful"])
    closer.add_argument("--notes", required=True)
    canceller = sub.add_parser("cancel")
    canceller.add_argument("--notes", required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("code", choices=["successful", "unsuccessful"])
    rollback.add_argument("--notes", required=True)
    args = parser.parse_args()

    state = run_state.load()
    if args.command in ("note", "cancel") and not state.get("change"):
        return  # nothing to write to before the change exists, so no need to wake ServiceNow
    sn, ssm = connect()
    if args.command == "open":
        open_change(sn, ssm, state)
    elif args.command == "note":
        note(sn, state, args.text)
    elif args.command == "wait":
        wait(sn, state, args.timeout, args.interval)
    elif args.command == "implement":
        implement(sn, state)
    elif args.command == "close":
        close(sn, state, args.code, args.notes)
    elif args.command == "rollback":
        record_rollback(sn, ssm, state, args.code, args.notes)
    else:
        cancel(sn, state, args.notes)


if __name__ == "__main__":
    sys.exit(main())
