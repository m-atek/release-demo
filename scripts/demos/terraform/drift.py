"""The drift check: does the network stack in AWS still match main?

    python scripts/demos/terraform/drift.py plan      in the toolchain image: terraform plan -detailed-exitcode
    python scripts/demos/terraform/drift.py report    on the host: Slack and a ServiceNow incident if it differs

The Jenkins job network-drift runs both on main at 07:00 on weekdays, when the
tools host is up, or when started by hand. Exit code 2 from the plan means
the stack differs from main. What changed outside Terraform, such as an
inbound rule added in the console, goes to Slack, and an incident opens in
ServiceNow through the Table API; while that incident is still open, later
runs add a work note to it instead of opening another.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import stack
import notify
import events
from common import fail, get_parameter, git, ssm_client

RESULT = stack.WORK / "drift.json"
PLAN_JSON = stack.WORK / "drift-plan.json"
DATA = stack.WORK / "terraform" / "drift"
INCIDENT_TITLE = "Drift in network-stack: changed outside Terraform"


# Reading the plan

def rule_text(rule, word):
    protocol = rule.get("protocol", "")
    if protocol == "-1":
        what = "all traffic"
    else:
        ports = rule.get("from_port"), rule.get("to_port")
        what = f"{protocol} {ports[0]}" if ports[0] == ports[1] else f"{protocol} {ports[0]}-{ports[1]}"
    places = (rule.get("cidr_blocks") or []) + (rule.get("ipv6_cidr_blocks") or []) + \
        (rule.get("prefix_list_ids") or []) + (rule.get("security_groups") or []) + (["itself"] if rule.get("self") else [])
    description = f" (\"{rule['description']}\")" if rule.get("description") else ""
    return f"{what} {word} {', '.join(places) or 'nowhere'}{description}"


def shown(value):
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= 60 else text[:57] + "..."


def differences(address, before, after):
    """What changed in one resource, in words: security group rules one by one."""
    lines = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new or key == "tags_all":
            continue
        if key in ("ingress", "egress") and isinstance(old, list) and isinstance(new, list):
            direction, word = ("inbound", "from") if key == "ingress" else ("outbound", "to")
            lines += [f"{address}: {direction} rule added: {rule_text(r, word)}" for r in new if r not in old]
            lines += [f"{address}: {direction} rule removed: {rule_text(r, word)}" for r in old if r not in new]
        elif key == "tags" and isinstance(old or {}, dict) and isinstance(new or {}, dict):
            old, new = old or {}, new or {}
            for tag in sorted(set(old) | set(new)):
                if old.get(tag) != new.get(tag):
                    lines.append(f"{address}: tag {tag} changed from {shown(old.get(tag, 'none'))} to {shown(new.get(tag, 'none'))}")
        else:
            lines.append(f"{address}: {key} changed from {shown(old)} to {shown(new)}")
    return lines


def describe(plan):
    """Changed outside Terraform (the plan's resource_drift), in words."""
    lines = []
    for drift in plan.get("resource_drift", []):
        change, address = drift["change"], drift["address"]
        if change["actions"] == ["delete"]:
            lines.append(f"{address}: deleted outside Terraform")
        else:
            lines += differences(address, change.get("before") or {}, change.get("after") or {})
    return lines


# Steps

def plan(args):
    """terraform plan -detailed-exitcode against main, as the plan role, without the
    state lock: it only reads, and never holds up a pull request's apply."""
    stack.init(DATA, stack.PROFILES["plan"])
    plan_file = stack.WORK / "drift.tfplan"
    result = stack.terraform("plan", "-detailed-exitcode", "-input=false", "-no-color", "-lock=false",
                             f"-out={plan_file}", data_dir=DATA, profile=stack.PROFILES["plan"], capture=True,
                             check=False)
    report = {"exit_code": result.returncode}
    if result.returncode in (0, 2):
        shown_plan = stack.terraform("show", "-json", str(plan_file), data_dir=DATA, profile=stack.PROFILES["plan"],
                                     capture=True)
        PLAN_JSON.write_text(shown_plan.stdout)
        text = stack.terraform("show", "-no-color", str(plan_file), data_dir=DATA, profile=stack.PROFILES["plan"],
                               capture=True)
        (stack.WORK / "reports").mkdir(parents=True, exist_ok=True)
        (stack.WORK / "reports" / "drift.txt").write_text(text.stdout)
    else:
        error = (result.stderr or result.stdout).strip()
        report["error"] = error.splitlines()[0] if error else "terraform plan failed"
        print(error, file=sys.stderr)
    stack.write_json(RESULT, report)
    print({0: "No drift: the stack matches main.", 2: "Drift: the stack differs from main."}.get(
        result.returncode, "The drift check failed."))


def open_incident(lines, summary, build_url):
    """A new incident, or a work note on the one still open. Returns (number, url, new)."""
    from change import connect

    sn, _ = connect()
    item = sn.find("cmdb_ci_appl", "name=network-stack", "sys_id")
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")
    caller = sn.find("sys_user", "user_name=release.pipeline", "sys_id")
    text = ("The morning drift check found the network stack in AWS different from main.\n\n"
            "Changed outside Terraform:\n" + "\n".join(f"- {line}" for line in lines or ["nothing, but main differs"])
            + f"\n\nTo match main again, Terraform would: {summary}.\nPlan: attached as drift.txt."
            f"\nBuild: {build_url}\n\nTo fix: revert the change by hand, or bring it into the code in a pull request. "
            "reset.py terraform applies main again.")
    query = f"active=true^short_description={INCIDENT_TITLE}" + (f"^cmdb_ci={item['sys_id']}" if item else "")
    existing = sn.find("incident", query, "sys_id,number")
    if existing:
        sn.call("PATCH", f"/api/now/table/incident/{existing['sys_id']}", {"work_notes": "Still drifting.\n\n" + text})
        return existing["number"], f"{sn.url}/incident.do?sys_id={existing['sys_id']}", False
    fields = {"short_description": INCIDENT_TITLE, "description": text, "category": "network",
              "impact": "2", "urgency": "2", "assignment_group": group["sys_id"] if group else "",
              "caller_id": caller["sys_id"] if caller else ""}
    if item:
        fields["cmdb_ci"] = item["sys_id"]
    created = sn.call("POST", "/api/now/table/incident", fields)
    report = stack.WORK / "reports" / "drift.txt"
    if report.exists():
        attach(sn, created["sys_id"], report)
    return created["number"], f"{sn.url}/incident.do?sys_id={created['sys_id']}", True


def attach(sn, incident_id, path):
    """The plan as text on the incident, through the Attachment API, as change.py attaches to changes."""
    url = (f"{sn.url}/api/now/attachment/file?table_name=incident&table_sys_id={incident_id}"
           f"&file_name={urllib.parse.quote(path.name)}")
    request = urllib.request.Request(url, data=path.read_bytes(), method="POST", headers={
        "Authorization": sn.auth, "Content-Type": "text/plain", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60):
        pass


def report(args):
    result = stack.read_json(RESULT)
    if not result:
        fail("No drift result: run drift.py plan first")
    ssm = ssm_client()
    build = {"number": os.environ.get("BUILD_NUMBER", "local"), "url": os.environ.get("BUILD_URL", "")}
    commit = os.environ.get("GIT_COMMIT") or git("rev-parse", "HEAD").stdout.strip()
    by_timer = os.environ.get("DRIFT_TRIGGER") == "timer"
    when = "the 07:00 check on main" if by_timer else "the drift check on main (it runs weekdays at 07:00; this run was started by hand)"
    token = get_parameter(ssm, "slack/bot-token")
    channel = get_parameter(ssm, "slack/channel-id")
    link = f"<{build['url']}|Drift check #{build['number']}>" if build["url"] else f"Drift check #{build['number']}"

    code = result["exit_code"]
    if code == 0:
        send(ssm, build, commit, "ok")
        print("No drift: the stack matches main. Nothing posted.")
        return
    if code != 2:
        notify.slack("chat.postMessage", token, channel=channel, unfurl_links=False,
                     text=f":warning: *The network-stack drift check couldn't run*: {result.get('error', 'unknown')}\n{link}")
        send(ssm, build, commit, "failed")
        fail(f"The drift check failed: {result.get('error')}")

    plan = stack.read_json(PLAN_JSON, {})
    lines = describe(plan)
    summary = stack.summary_line(stack.plan_summary(plan))
    number, url, new = open_incident(lines, summary, build["url"])
    shown_lines = "\n".join(f"• {line}" for line in lines) or "• Nothing changed by hand, but the stack differs from main."
    text = (f":warning: *Drift in network-stack*: AWS no longer matches main.\n{shown_lines}\n"
            f"To match main again, Terraform would: {summary}.\n"
            f"Found by {when}. {link} · <{url}|{number}>{'' if new else ' (still open, updated)'}")
    notify.slack("chat.postMessage", token, channel=channel, text=text, unfurl_links=False)
    send(ssm, build, commit, "drift", incident=number)
    print(text.replace("*", ""))


def send(ssm, build, commit, result, incident=None):
    body = {"build": build["number"], "build_url": build["url"], "service": stack.SERVICE, "version": f"main {commit[:7]}",
            "environment": "production", "change": None, "jira_key": None, "stage": "Drift check", "result": result,
            "duration_seconds": None, "incident": incident, "time": int(time.time())}
    print(f"event Drift check {result}: Splunk {events.to_splunk(ssm, body)}, Grafana {events.to_loki(ssm, body)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["plan", "report"])
    args = parser.parse_args()
    {"plan": plan, "report": report}[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
