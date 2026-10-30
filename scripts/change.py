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
is refused, whatever ServiceNow says, and so is one recorded by a machine
user (release.pipeline, ai.notes or evidence.reader) or by a coding agent. A
pull request written by a coding agent needs a person's approving review on
GitHub before the change opens (scripts/authorship.py).

With the run state cost_review (video 18's cost check, above its limit or
without a measurement), the change is a normal one even when the option asks
for standard, and its field Cost review says whom ServiceNow asks: "Cost
approvers" when the option asked for a standard change, "Cost and change
approvers" when it asked for a normal one. cost.json joins the attachments
when it exists.
"""

import argparse
import mimetypes
import re
import sys
import time
import urllib.parse
import urllib.request

import run_state
from common import ROOT, fail, get_parameter, people, service, ssm_client
from servicenow_setup import ServiceNow

ASSESS, SCHEDULED, IMPLEMENT, REVIEW, CLOSED, CANCELED = "-4", "-2", "-1", "0", "3", "4"
EVIDENCE = ["junit.xml", "sbom.cdx.json", "grype.json", "build-info.json", "cost.json"]
# Users that may write to a change but never approve one (videos 11, 12 and 13).
MACHINE_USERS = ["release.pipeline", "ai.notes", "evidence.reader"]
# u_cost_review's values (servicenow_setup.py), by whether a standard change was asked for.
COST_REVIEW = {True: "cost_approvers", False: "cost_and_change_approvers"}
REJECTION_COMMENT_LIMIT = 120


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


def code_review(state):
    """Video 16: who wrote the release's code and who reviewed it (scripts/authorship.py), for a
    service with pull_requests in services.yaml and a release built from a commit; else None.
    When GitHub or AWS doesn't answer, the change says so and the release goes on."""
    import authorship

    if not authorship.SHA.fullmatch(state.get("commit") or ""):
        return None
    try:
        found = authorship.for_release(state)
    except (Exception, SystemExit) as error:
        return {"fields": {}, "refused": [],
                "lines": [f"Pull requests not read from GitHub ({error.__class__.__name__}): code authorship not recorded."]}
    if found is None:
        return None
    fields, lines = authorship.record(found)
    return {"fields": fields, "lines": lines, "refused": found["refused"]}


def refuse_unreviewed(state, lines):
    """The Change stage stops before the change opens; Slack says why, then stopped() says where."""
    import notify

    state.setdefault("lines", []).extend(lines)
    run_state.save(state)
    notify.post(state)
    fail(lines[0])


def open_change(sn, ssm, state):
    """The change, plus what a deploy script may have put in the run state:
    change_extras (fields for the change, and affected_cis, the sys_ids of the
    CMDB items it touches) and change_details (lines added to the description,
    so the approver sees them), and change_work_notes (lines for one work note
    once it exists). The servers a service lists in services.yaml
    (servicenow.servers, CI names of any class) are affected items too."""
    config = service(state["service"])
    code = code_review(state)
    if code and code["refused"]:
        refuse_unreviewed(state, code["refused"])
    # A service may release as standard changes by default (services.yaml servicenow.change),
    # or name the Slack options that make one (servicenow.standard_options, by default standard).
    standard = (state.get("option") in config["servicenow"].get("standard_options", ["standard"])
                or config["servicenow"].get("change") == "standard")
    # Video 18: a cost check above its limit, or without a measurement, makes it a normal change.
    cost_review = COST_REVIEW[standard] if state.get("cost_review") else None
    standard = standard and not cost_review
    groups = {g: sn.find("sys_user_group", f"name={g}", "sys_id")["sys_id"] for g in ("Platform team",)}
    ci = sn.find("cmdb_ci_appl", f"name={config['servicenow']['cmdb_item']}", "sys_id")["sys_id"]
    asked_by = requester_user(sn, state.get("requested_by"))
    started = run_state.now()
    fields = {
        "short_description": f"Release {state['service']} {state['version']} ({state['jira_key']})",
        "description": f"Release of {state['service']} {state['version']} for {state['jira_key']}, started from Slack "
                       f"and run by the release pipeline.",
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
    extras = state.get("change_extras") or {}
    fields.update(extras.get("fields") or {})
    if cost_review:
        fields["u_cost_review"] = cost_review
    if state.get("change_details"):
        fields["description"] += "\n\n" + "\n".join(state["change_details"])
    if code:
        fields.update(code["fields"])
        fields["description"] += "\n\n" + "\n".join(code["lines"])
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
    if state.get("change_work_notes"):
        note(sn, state, "\n".join(state["change_work_notes"]))
    # Affected items go on while the change is New: in Assess ServiceNow refuses them.
    affected = list(extras.get("affected_cis") or [])
    for name in config["servicenow"].get("servers", []):
        server = sn.find("cmdb_ci", f"name={name}", "sys_id")
        if server is None:
            print(f"No CI {name} in ServiceNow: not on the change as an affected item.")
        elif server["sys_id"] not in affected:
            affected.append(server["sys_id"])
    for ci_id in affected:
        sn.call("POST", "/api/now/table/task_ci", {"task": change_id, "ci_item": ci_id})
    if not standard:
        sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": ASSESS})
    waiting = {"cost_approvers": "Cost approvers", "cost_and_change_approvers": "Cost approvers and Change approvers"}
    print(f"{number}: standard, pre-approved" if standard else
          f"{number}: normal, waiting for {waiting.get(cost_review, 'Change approvers')}")


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


def first_name(sn, sys_id):
    user = sn.find("sys_user", f"sys_id={sys_id}", "first_name,name") or {}
    return user.get("first_name") or user.get("name") or "someone"


def names(people):
    return people[0] if len(people) == 1 else ", ".join(people[:-1]) + " and " + people[-1]


def journal_text(value):
    """The newest entry of a journal field as the Table API shows it: its text
    without the "<time> - <name> (Comments)" line ServiceNow puts above it."""
    entries = re.split(r"(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - .*\n", (value or "").strip())
    texts = [entry.strip() for entry in entries if entry.strip()]
    return texts[0] if texts else ""


def rejection_comment(sn, approval):
    """What the approver wrote when rejecting: the approval record's comments,
    or, where the API leaves that field empty, its newest journal entry."""
    record = sn.call("GET", f"/api/now/table/sysapproval_approver/{approval['sys_id']}", sysparm_fields="comments",
                     sysparm_display_value="true") or {}
    text = journal_text(record.get("comments"))
    if not text:
        entry = sn.find("sys_journal_field", f"element_id={approval['sys_id']}^element=comments^ORDERBYDESCsys_created_on",
                        "value")
        text = (entry or {}).get("value", "").strip()
    return " ".join(text.split())


def rejection_line(sn, change):
    """Slack's line for a rejection: "Rejected by Alex in ServiceNow: <comment>", cut at 120 characters."""
    rejected = sn.call("GET", "/api/now/table/sysapproval_approver", sysparm_query=f"sysapproval={change['sys_id']}^state=rejected",
                       sysparm_fields="sys_id,approver") or []
    if not rejected:
        return "Rejected in ServiceNow."
    name = first_name(sn, rejected[0]["approver"]["value"])
    comment = rejection_comment(sn, rejected[0])
    if not comment:
        return f"Rejected by {name} in ServiceNow."
    if len(comment) > REJECTION_COMMENT_LIMIT:
        comment = comment[:REJECTION_COMMENT_LIMIT - 3].rstrip() + "..."
    return f"Rejected by {name} in ServiceNow: {comment}"


def machine_ids(sn):
    found = (sn.find("sys_user", f"user_name={user}", "sys_id") for user in MACHINE_USERS)
    return {user["sys_id"] for user in found if user}


def wait(sn, state, timeout, interval):
    from notify import post

    change = state["change"]
    machines, agents = machine_ids(sn), agent_users(sn)
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = sn.call("GET", f"/api/now/table/change_request/{change['sys_id']}", sysparm_fields="approval,state")
        if record["approval"] == "rejected":
            state.setdefault("lines", []).append(rejection_line(sn, change))
            run_state.save(state)
            post(state)
            cancel(sn, state, "Rejected in ServiceNow. The release stopped before production.")
            fail("The change was rejected.")
        approvals = sn.call("GET", "/api/now/table/sysapproval_approver", sysparm_query=f"sysapproval={change['sys_id']}^state=approved",
                            sysparm_fields="approver")
        approvers = [a["approver"]["value"] for a in approvals]
        if change["requested_by"] in approvers:
            cancel(sn, state, "Approved by the person who asked for the release. Refused by the pipeline: "
                              "the person who asks can't approve.")
            fail("The change was approved by the person who asked for it. Refused.")
        if machines & set(approvers):
            cancel(sn, state, "Approved by a machine user. Refused by the pipeline: only a person approves a change.")
            fail("The change was approved by a machine user. Refused.")
        if agents & set(approvers):
            cancel(sn, state, "Approved by a coding agent's ServiceNow user. Refused by the pipeline: "
                              "an agent never approves a release.")
            fail("The change was approved by a coding agent. Refused.")
        if record["approval"] == "approved" and approvers:
            state["change"]["approved_by"] = names([first_name(sn, sys_id) for sys_id in dict.fromkeys(approvers)])
            run_state.save(state)
            print(f"Approved by {state['change']['approved_by']} in ServiceNow.")
            return
        time.sleep(interval)
    cancel(sn, state, f"No approval within {timeout // 60} minutes. The release stopped before production.")
    fail("No approval in time.")


def agent_users(sn):
    """The ServiceNow users of people.yaml's agent: true entries. The coding agent has none
    today, so this guards the day one gets an account."""
    from authorship import everyone

    names = [p["servicenow_user"] for p in everyone().values() if p.get("agent") and p.get("servicenow_user")]
    found = (sn.find("sys_user", f"user_name={name}", "sys_id") for name in names)
    return {user["sys_id"] for user in found if user}


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
                       "release before it, and checked it. Recorded by the rollback job.",
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
    """Cancel the change. One cancelled already keeps its first close notes,
    so a rejection's stays when the stopped run cancels again: it gets the note only."""
    change = state.get("change")
    if change:
        note(sn, state, notes)
        current = sn.call("GET", f"/api/now/table/change_request/{change['sys_id']}", sysparm_fields="state") or {}
        if current.get("state") == CANCELED:
            return
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
