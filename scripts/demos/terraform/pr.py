"""The pull request pipeline for the network stack, one step per call, with
the same change, Slack and event steps as a release.

On the tools-host agent:
    pr.py init [--pr 7]                 run state from the pull request, and its Slack message
    pr.py sync                          the check results so far, to Slack
    pr.py stage Plan running|ok|failed|skipped [--seconds 12.3]
    pr.py gate                          stop when a check failed; the pull request and Slack name them
    pr.py has-changes                   exit 1 when the plan changes nothing
    pr.py comment                       the plan summary and the cost difference on the pull request
    pr.py change open|wait|implement    the normal change in ServiceNow, and Alex's approval
    pr.py verify                        the bucket and queue exist, and the bucket blocks public access
    pr.py merge                         merge the pull request through the GitHub App
    pr.py finish applied|no-changes
    pr.py stopped "reason"
In the Terraform toolchain image:
    pr.py plan                          terraform plan -out to a file, its summary and the Plan check
    pr.py cost                          Infracost on main and on the pull request, and the difference
    pr.py apply                         terraform apply of that saved file; exit 3 if the state changed since

The run state is .release/state.json, as for a release. The plan file never
leaves the workspace: the change gets terraform show -no-color, which masks
sensitive values, and the public pull request gets the summary only.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation

import stack
import checks
import github
import change as servicenow_change
import events
import notify
from common import fail, get_parameter, people, service, ssm_client

STAGES = ["Checks", "Plan", "Cost", "Change", "Approval", "Apply", "Merge"]
PLAN_FILE = stack.WORK / "network.tfplan"
PLAN_JSON = stack.WORK / "plan.json"
PLAN_TEXT = stack.WORK / "reports" / "plan.txt"
OUTPUTS = stack.WORK / "outputs.json"
PLAN_DATA = stack.WORK / "terraform" / "plan"
STALE = 3
STALE_MESSAGE = ("Terraform refused the saved plan: the state changed since the plan was made. Nothing was applied. "
                 "Push a commit or run this pull request's build again for a new plan and approval.")


# Slack: one message per build, rewritten at every step.

def requester_text(state):
    return f"<@{state['requester_slack']}>" if state.get("requester_slack") else state.get("requester_name", "")


def render(state):
    pr = state["pull_request"]
    head = (f"*Infrastructure change: {pr['title']}*\n{stack.SERVICE} · <{pr['url']}|pull request #{pr['number']}> · "
            f"commit `{state['commit'][:7]}` · requested by {requester_text(state)}")
    found = state.get("checks", {})
    marks = []
    for name in checks.CHECKS:
        conclusion = found.get(name, {}).get("conclusion")
        marks.append(f"{notify.ICON['ok'] if conclusion == 'success' else notify.ICON['failed'] if conclusion else '○'} {name}")
    rows = ["Checks:  " + "   ".join(marks)]
    for name in checks.CHECKS:
        failures = found.get(name, {}).get("failures") or []
        if found.get(name, {}).get("conclusion") == "failure":
            shown = ", ".join(failures[:6]) + (f" and {len(failures) - 6} more" if len(failures) > 6 else "")
            rows.append(f"{notify.ICON['failed']}  *{name}*: {shown}")
    for name in STAGES[1:]:
        stage = state["stages"][name]
        took = f"  _{stack.run_state.duration(stage['seconds'])}_" if stage.get("seconds") else ""
        detail = f"  ·  {stage['detail']}" if stage.get("detail") else ""
        rows.append(f"{notify.ICON[stage['status']]}  {name}{took}{detail}")
    links = []
    if state["build"].get("url"):
        links.append(f"<{state['build']['url']}|Build #{state['build']['number']}>")
    if state.get("change", {}).get("url"):
        links.append(f"<{state['change']['url']}|{state['change']['number']}>")
    lines = [head, "\n".join(rows)]
    if links:
        lines.append(" · ".join(links))
    lines += state.get("lines", [])
    return "\n\n".join(lines)


def post(state, ssm=None):
    ssm = ssm or ssm_client()
    token = get_parameter(ssm, "slack/bot-token")
    if state["slack"].get("ts"):
        notify.slack("chat.update", token, channel=state["slack"]["channel"], ts=state["slack"]["ts"], text=render(state))
        return
    channel = state["slack"].get("channel") or get_parameter(ssm, "slack/channel-id")
    reply = notify.slack("chat.postMessage", token, channel=channel, text=render(state), unfurl_links=False)
    if reply.get("ok"):
        state["slack"] = {"channel": reply["channel"], "ts": reply["ts"]}
        stack.save(state)


# Events, in the shape scripts/events.py sends for releases.

def send_event(ssm, state, stage, result, seconds=None, deploy=False):
    body = {
        "build": state["build"]["number"], "build_url": state["build"].get("url"),
        "service": stack.SERVICE, "version": state["version"], "environment": "production" if deploy else None,
        "change": state.get("change", {}).get("number"), "jira_key": state.get("jira_key"),
        "stage": stage, "result": result, "duration_seconds": seconds,
        "requested_by": state.get("requested_by"), "option": "", "pull_request": state["pull_request"]["number"],
    }
    if deploy:
        body["deploy"] = True
        body["commit_time"] = state.get("commit_time")
        if state.get("commit_time"):
            body["lead_time_seconds"] = round(time.time() - state["commit_time"])
    print(f"event {stage} {result}: Splunk {events.to_splunk(ssm, body)}, Grafana {events.to_loki(ssm, body)}")


def note(state, text):
    if state.get("change"):
        sn, _ = servicenow_change.connect()
        servicenow_change.note(sn, state, text)


# Steps on the host

def init(args):
    number = args.pr or os.environ.get("CHANGE_ID")
    if not number:
        fail("No pull request: this runs in the network-stack job, or by hand with --pr <number>.")
    ssm = ssm_client()
    gh = github.GitHub(ssm)
    pull = gh.pull(number)
    head = gh.commit(pull["head"]["sha"])
    key, person = github.requester(pull, head)
    committed = head["commit"]["committer"]["date"]
    jira = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", pull["title"])
    state = {
        "service": stack.SERVICE,
        "kind": "pull-request",
        "version": f"PR #{number} {pull['head']['sha'][:7]}",
        "pull_request": {"number": int(number), "title": pull["title"], "url": pull["html_url"],
                         "head_sha": pull["head"]["sha"], "branch": pull["head"]["ref"]},
        "commit": pull["head"]["sha"],
        "commit_time": int(datetime.datetime.fromisoformat(committed.replace("Z", "+00:00")).timestamp()),
        "jira_key": jira.group(0) if jira else "",
        "requested_by": key or "",
        "requester_name": person["display_name"],
        "requester_slack": people(ssm).get(key, {}).get("slack_user_id") if key else None,
        "build": {"number": os.environ.get("BUILD_NUMBER", "local"), "url": os.environ.get("BUILD_URL", "")},
        "slack": {"channel": "", "ts": ""},
        "started_at": time.time(),
        "stages": {name: {"status": "waiting"} for name in STAGES},
        "checks": {},
        "lines": [],
    }
    stack.save(state)
    (stack.WORK / "reports").mkdir(parents=True, exist_ok=True)
    stack.write_backend(ssm)
    post(state, ssm)
    print(f"Pull request #{number}: {pull['title']}, head {pull['head']['sha'][:7]}, asked by {person['display_name']}")


def sync_checks(state):
    for name in checks.CHECKS + ["Plan"]:
        found = stack.read_json(checks.result_path(name))
        if found:
            state["checks"][name] = {k: found[k] for k in ("conclusion", "title", "failures")}


def stage(args):
    ssm = ssm_client()
    state = stack.load()
    sync_checks(state)
    current = state["stages"][args.name]
    current["status"] = args.status
    if args.status == "running":
        current["started"] = time.time()
    elif args.seconds is None and current.get("started"):
        args.seconds = round(time.time() - current["started"], 1)
    if args.seconds is not None:
        current["seconds"] = args.seconds
    if args.name == "Plan" and state.get("plan", {}).get("resources") is not None:
        current["detail"] = stack.summary_line(state["plan"])
    if args.name == "Cost" and state.get("cost"):
        current["detail"] = cost_line(state["cost"])
    stack.save(state)
    post(state, ssm)
    if args.status in ("ok", "failed", "skipped"):
        send_event(ssm, state, args.name, args.status, args.seconds, deploy=args.name == "Apply" and args.status == "ok")
        took = f" in {args.seconds:.1f}s" if args.seconds is not None else ""
        note(state, f"{args.name}: {args.status}{took}" + (f". {current['detail']}" if current.get("detail") else ""))


def sync(args):
    state = stack.load()
    sync_checks(state)
    stack.save(state)
    post(state)


def gate(args):
    """After the checks and the plan: every check passed, or Slack says which failed, by name."""
    ssm = ssm_client()
    state = stack.load()
    sync_checks(state)
    failed = [name for name in checks.CHECKS if state["checks"].get(name, {}).get("conclusion") != "success"]
    state["stages"]["Checks"]["status"] = "failed" if failed else "ok"
    if not failed:
        stack.save(state)
        send_event(ssm, state, "Checks", "ok")
        print("Every check passed.")
        return
    if state.get("plan", {}).get("error"):
        state["lines"].append(f"*Blocked*: terraform plan failed: {state['plan']['error']}")
    else:
        names = " and ".join([", ".join(failed[:-1]), failed[-1]] if len(failed) > 1 else failed)
        until = "they pass" if len(failed) > 1 else "it passes"
        state["lines"].append(f"*Blocked*: {names} failed. The merge stays blocked until {until}.")
    state["result"] = "blocked"
    stack.save(state)
    post(state, ssm)
    send_event(ssm, state, "Checks", "failed")
    fail(f"Checks failed: {', '.join(failed)}")


def cost_line(cost):
    if cost.get("status") != "priced":
        return f"not priced: {cost.get('reason', 'unknown')}"
    return f"{stack.money(cost['difference'], sign=True)} a month"


def comment_body(state, status):
    plan, cost, pr = state.get("plan", {}), state.get("cost", {}), state["pull_request"]
    lines = ["### Terraform plan for network-stack", ""]
    priced = cost.get("status") == "priced"
    money = (f"monthly cost **{stack.money(cost['difference'], sign=True)}** (main {stack.money(cost['main'])}, "
             f"this pull request {stack.money(cost['branch'])})" if priced else f"monthly cost {cost_line(cost)}")
    lines.append(f"**{stack.summary_line(plan)}** · {money}")
    if plan.get("resources"):
        lines += ["", "| Action | Resource |", "| --- | --- |"]
        lines += [f"| {stack.SYMBOLS[r['action']]} {r['action']} | `{r['address']}` |" for r in plan["resources"]]
    marks = [f"{'✅' if state['checks'].get(n, {}).get('conclusion') == 'success' else '❌'} {n}" for n in checks.CHECKS]
    lines += ["", "Checks: " + " · ".join(marks)]
    if priced:
        lines += ["", "Prices from Infracost, with the expected use in "
                      "`infra/demos/2-terraform/network/infracost-usage.yml`."]
    lines += ["", status, "",
              f"<sub>Jenkins build #{state['build']['number']}, commit {state['commit'][:7]}.</sub>"]
    return "\n".join(lines)


def comment(args, status=None):
    state = stack.load()
    sync_checks(state)
    if status is None:
        status = ("**Next:** a normal change in ServiceNow. After Change approvers approve it, Jenkins applies "
                  "exactly this plan and merges this pull request." if stack.has_changes(state["plan"])
                  else "No changes to apply, so nothing to approve.")
    github.GitHub().upsert_comment(state["pull_request"]["number"], comment_body(state, status))
    print(f"Commented on pull request #{state['pull_request']['number']}")


def change_open(state, ssm):
    """A normal change with the plan summary, the plan text, the cost difference and the check results."""
    sn, _ = servicenow_change.connect()
    config = service(stack.SERVICE)["servicenow"]
    pr, plan, cost = state["pull_request"], state["plan"], state.get("cost", {})
    group = sn.find("sys_user_group", f"name={config['assignment_group']}", "sys_id")["sys_id"]
    item = sn.find("cmdb_ci_appl", f"name={config['cmdb_item']}", "sys_id")
    if not item:
        fail(f"No CMDB item {config['cmdb_item']} in ServiceNow yet: python scripts/servicenow_setup.py")
    asked_by = sn.find("sys_user", f"user_name={people(ssm)[state['requested_by']]['servicenow_user']}", "sys_id") \
        if state.get("requested_by") else sn.find("sys_user", "user_name=release.pipeline", "sys_id")
    jira_url = f"{get_parameter(ssm, 'jira/site-url').rstrip('/')}/browse/{state['jira_key']}" if state.get("jira_key") else ""
    resources = "\n".join(f"{stack.SYMBOLS[r['action']]} {r['action']} {r['address']}" for r in plan["resources"])
    check_lines = "\n".join(f"{'Passed' if c['conclusion'] == 'success' else 'FAILED'}: {name}: {c['title']}"
                            for name, c in state["checks"].items())
    cost_text = (f"Monthly cost difference: {stack.money(cost['difference'], sign=True)} "
                 f"(main {stack.money(cost['main'])}, this change {stack.money(cost['branch'])}), from Infracost with "
                 "the expected use in infracost-usage.yml." if cost.get("status") == "priced"
                 else f"Monthly cost: {cost_line(cost)}.")
    started = time.time()
    fields = {
        "short_description": f"{pr['title']} (network-stack, pull request #{pr['number']})",
        "description": (f"Infrastructure change to network-stack from pull request #{pr['number']}, checked, planned "
                        f"and priced by Jenkins.\n\nPlan: {stack.summary_line(plan)}\n"
                        f"{resources}\n\n{cost_text}\n\nThe full plan is attached as plan.txt (terraform show "
                        "-no-color, sensitive values masked)."),
        "cmdb_ci": item["sys_id"], "assignment_group": group, "requested_by": asked_by["sys_id"],
        "category": "Network",
        "justification": "\n".join(filter(None, [f"{state['jira_key']}: {jira_url}" if jira_url else "",
                                                 f"Pull request: {pr['url']}"])),
        "implementation_plan": (f"Jenkins applies the plan file saved by build #{state['build']['number']}, exactly as "
                                "approved: terraform apply <plan file>. If the state changed since the plan, "
                                "Terraform refuses it and nothing is applied. Then Jenkins checks that the bucket "
                                "and queue exist and that the bucket blocks public access, and merges the pull "
                                f"request.\nBuild: {state['build'].get('url', '')}"),
        "backout_plan": ("Revert the pull request on GitHub. The same pipeline checks and plans the revert, opens a "
                         "change for it, and applies it after approval."),
        "test_plan": f"Checks on the pull request:\n{check_lines}\n\nPlan: {stack.summary_line(plan)}\n{cost_text}",
        "risk_impact_analysis": f"{stack.summary_line(plan)}. {cost_text}",
        "start_date": stack.run_state.iso(started).replace("T", " ").rstrip("Z"),
        "end_date": stack.run_state.iso(started + 3600).replace("T", " ").rstrip("Z"),
    }
    reply = sn.call("POST", "/api/sn_chg_rest/change/normal", fields)
    change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
    state["change"] = {"sys_id": change_id, "number": number, "type": "normal",
                       "url": f"{sn.url}/change_request.do?sys_id={change_id}", "requested_by": asked_by["sys_id"]}
    stack.save(state)

    evidence = stack.WORK / "reports"
    (evidence / "checks.txt").write_text(check_lines + "\n")
    (evidence / "cost.txt").write_text(cost_text + "\n")
    for name in ("plan.txt", "checks.txt", "cost.txt"):
        servicenow_change.attach(sn, change_id, evidence / name)
    servicenow_change.note(sn, state, f"Evidence:\nPull request: {pr['url']}\nBuild: {state['build'].get('url', '')}\n"
                                      f"Jira: {jira_url or 'none'}\nAttached: plan.txt, checks.txt, cost.txt")
    sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": servicenow_change.ASSESS})
    print(f"{number}: normal, waiting for Change approvers")


def change_step(args):
    ssm = ssm_client()
    state = stack.load()
    if args.step == "open":
        change_open(state, ssm)
        post(state, ssm)
        comment(None, status=f"**Waiting for approval:** change {state['change']['number']} in ServiceNow. After "
                             "Change approvers approve it, Jenkins applies exactly this plan and merges this pull request.")
    elif args.step == "wait":
        sn, _ = servicenow_change.connect()
        servicenow_change.wait(sn, state, args.timeout, args.interval)
        state = stack.load()
        state["lines"].append(f"Approved by {state['change']['approved_by']} in ServiceNow.")
        stack.save(state)
        post(state, ssm)
    else:
        sn, _ = servicenow_change.connect()
        servicenow_change.implement(sn, state)
        state["apply_started"] = time.time()
        stack.save(state)


def verify(args):
    """The resources the stack's outputs name, checked as the plan role: one line each, like check.py."""
    from botocore.exceptions import ClientError

    outputs = {k: v["value"] for k, v in (stack.read_json(OUTPUTS, {}) or {}).items()}
    bucket, queue = outputs.get("exports_bucket"), outputs.get("exports_queue_url")
    if not bucket and not queue:
        print("ok   nothing to check: the stack has no exports bucket or queue")
        return
    session = stack.role_session("plan")
    s3, sqs = session.client("s3"), session.client("sqs")
    results = []
    if bucket:
        try:
            s3.head_bucket(Bucket=bucket)
            results.append((True, "the exports bucket exists"))
            block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
            results.append((all(block.values()) and len(block) == 4, "the exports bucket blocks public access"))
            try:
                public = s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"]["IsPublic"]
            except ClientError as error:
                public = error.response["Error"]["Code"] != "NoSuchBucketPolicy"
            results.append((not public, "no bucket policy makes it public"))
        except ClientError as error:
            results.append((False, f"the exports bucket: {error.response['Error']['Code']}"))
    if queue:
        try:
            sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])
            results.append((True, "the exports queue exists"))
        except ClientError as error:
            results.append((False, f"the exports queue: {error.response['Error']['Code']}"))
    for ok, what in results:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not all(ok for ok, _ in results):
        fail("The applied stack isn't as planned.")


def has_changes(args):
    """Exit 0 when the plan changes something, 1 when there is nothing to apply."""
    sys.exit(0 if stack.has_changes(stack.load()["plan"]) else 1)


def merge(args):
    state = stack.load()
    gh = github.GitHub()
    pr = state["pull_request"]
    current = gh.pull(pr["number"])
    if current["state"] != "open" or current["head"]["sha"] != pr["head_sha"]:
        fail(f"Pull request #{pr['number']} changed after the plan: not merged. The approved plan is applied; "
             "merge it by hand once its checks pass.")
    approver = state.get("change", {}).get("approved_by", "Change approvers")
    gh.merge(pr["number"], pr["head_sha"], f"{pr['title']} (#{pr['number']})",
             f"Applied by Jenkins build #{state['build']['number']} after {approver} approved "
             f"{state['change']['number']} in ServiceNow. {stack.summary_line(state['plan'])}.")
    print(f"Merged pull request #{pr['number']}")


def finish(args):
    ssm = ssm_client()
    state = stack.load()
    total = stack.run_state.duration(time.time() - state["started_at"])
    state["total"] = total
    pr = state["pull_request"]
    if args.result == "no-changes":
        for name in ("Cost", "Change", "Approval", "Apply", "Merge"):
            if state["stages"][name]["status"] == "waiting":
                state["stages"][name]["status"] = "skipped"
        state["lines"].append("No changes to apply, so nothing to approve.")
        state["result"] = "no-changes"
        stack.save(state)
        post(state, ssm)
        return
    state["lines"].append(f"*Applied* in {total}. Merged pull request #{pr['number']}.")
    state["result"] = "applied"
    stack.save(state)
    post(state, ssm)
    applied = state.get("apply", {}).get("summary", stack.summary_line(state["plan"]))
    sn, _ = servicenow_change.connect()
    servicenow_change.close(sn, state, "successful", f"Applied the approved plan from build #{state['build']['number']}: "
                            f"{applied} Checked the bucket and queue, and merged pull request #{pr['number']}. "
                            f"Total time: {total}. Build: {state['build'].get('url', '')}")
    comment(None, status=f"**Applied** by Jenkins build #{state['build']['number']} after "
                         f"{state['change'].get('approved_by', 'Change approvers')} approved {state['change']['number']}. "
                         "Merged.")
    if state.get("jira_key"):
        subprocess.run([sys.executable, str(stack.ROOT / "scripts" / "jira.py"), "comment",
                        f"Applied network-stack pull request #{pr['number']} in {state['change']['number']}: {applied} "
                        f"Total time: {total}. Build: {state['build'].get('url', '')}"], check=False)
    events.annotate(ssm, f"network-stack: {pr['title']} applied (pull request #{pr['number']})",
                    ["infrastructure", stack.SERVICE])


def stopped(args):
    """Any failure: Slack says where it stopped, and the change is cancelled, or closed
    as unsuccessful once the apply has started."""
    ssm = ssm_client()
    state = stack.load()
    if state.get("result"):
        return  # the gate or the finish already said so
    reason = args.reason.strip().splitlines()[0] if args.reason.strip() else "the build failed"
    state["result"] = "stopped"
    state["total"] = stack.run_state.duration(time.time() - state["started_at"])
    state["lines"].append(f"*Stopped* after {state['total']}: {reason}")
    stack.save(state)
    post(state, ssm)
    send_event(ssm, state, "Pipeline", "failed")
    if state.get("change"):
        sn, _ = servicenow_change.connect()
        if state.get("apply_started"):
            servicenow_change.close(sn, state, "unsuccessful", f"Stopped during the apply: {reason}")
        else:
            servicenow_change.cancel(sn, state, f"Stopped before the apply: {reason} Nothing changed.")
    if state.get("plan", {}).get("resources") is not None:
        comment(None, status=f"**Stopped:** {reason}")


# Steps in the toolchain container

def plan(args):
    state = stack.load()
    stack.init(PLAN_DATA, stack.PROFILES["plan"])
    result = stack.terraform("plan", "-input=false", "-no-color", "-lock-timeout=60s", f"-out={PLAN_FILE}",
                             data_dir=PLAN_DATA, profile=stack.PROFILES["plan"], capture=True, check=False)
    if result.returncode != 0:
        error = stack.scrub(result.stderr or result.stdout)
        state["plan"] = {"error": error.strip().splitlines()[0] if error.strip() else "terraform plan failed"}
        stack.save(state)
        checks.record("Plan", False, "terraform plan failed", "terraform plan failed, so there is nothing to apply.",
                      checks.fence(error[-20000:]), failures=["terraform plan failed"])
        fail("terraform plan failed")
    shown = stack.terraform("show", "-json", str(PLAN_FILE), data_dir=PLAN_DATA, profile=stack.PROFILES["plan"],
                            capture=True)
    PLAN_JSON.write_text(shown.stdout)
    text = stack.terraform("show", "-no-color", str(PLAN_FILE), data_dir=PLAN_DATA, profile=stack.PROFILES["plan"],
                           capture=True)
    PLAN_TEXT.parent.mkdir(parents=True, exist_ok=True)
    PLAN_TEXT.write_text(text.stdout)
    summary = stack.plan_summary(json.loads(shown.stdout))
    state["plan"] = summary
    stack.save(state)
    write_plan_check(state)
    print(stack.summary_line(summary))


def write_plan_check(state):
    summary, cost = state["plan"], state.get("cost")
    title = stack.summary_line(summary) + (f" · {cost_line(cost)}" if cost else "")
    table = "\n".join(f"| {stack.SYMBOLS[r['action']]} {r['action']} | `{r['address']}` |" for r in summary["resources"])
    text = ("| Action | Resource |\n| --- | --- |\n" + table) if table else "No changes."
    checks.record("Plan", True, title, f"terraform plan, saved to a file that the apply uses as it is. {title}.", text)


def infracost_total(directory, name):
    """Infracost's monthly total for one directory, from infracost scan --json."""
    try:
        result = subprocess.run(["infracost", "scan", str(directory), "--json"], capture_output=True, text=True,
                                timeout=240, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("no answer within 4 minutes (is the token valid?)")
    (stack.WORK / "reports" / f"infracost-{name}.json").write_text(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(stack.scrub((result.stderr or result.stdout).strip().splitlines()[-1:][0]
                                       if (result.stderr or result.stdout).strip() else "infracost scan failed"))
    report = json.loads(result.stdout[result.stdout.index("{"):])
    total = (report.get("summary") or {}).get("total_monthly_cost")
    if total is None:
        total = sum((Decimal(str(component.get("total_monthly_cost") or 0))
                     for project in report.get("projects", []) for resource in project.get("resources", [])
                     for component in resource.get("cost_components") or []), Decimal(0))
    try:
        return Decimal(str(total))
    except InvalidOperation:
        raise RuntimeError(f"Infracost gave no total for {name}")


def main_copy():
    """The stack as it is on main, next to the pull request's, for Infracost."""
    target = stack.WORK / "cost" / "main"
    subprocess.run(["rm", "-rf", str(target)], check=True)
    target.mkdir(parents=True)
    path = stack.relative(stack.STACK)
    for ref in ("refs/remotes/origin/main", "origin/main", "main"):
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=stack.ROOT,
                          capture_output=True).returncode == 0:
            break
    else:
        subprocess.run(["git", "fetch", "--quiet", "--depth=1", "origin", "main"], cwd=stack.ROOT, check=True)
        ref = "FETCH_HEAD"
    archive = subprocess.run(["git", "archive", "--format=tar", ref, "--", path], cwd=stack.ROOT, capture_output=True)
    if archive.returncode != 0:
        return None  # main has no stack yet: it costs nothing there
    subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
    return target / path


def cost(args):
    state = stack.load()
    token_file = stack.WORK / "secrets" / "infracost-token"
    if token_file.exists():
        os.environ["INFRACOST_CLI_AUTHENTICATION_TOKEN"] = token_file.read_text().strip()
    if not os.environ.get("INFRACOST_CLI_AUTHENTICATION_TOKEN"):
        state["cost"] = {"status": "unpriced", "reason": "no Infracost token in SSM yet (infracost/cli-token)"}
    else:
        try:
            base = main_copy()
            main_total = infracost_total(base, "main") if base else Decimal(0)
            branch_total = infracost_total(stack.STACK, "branch")
            state["cost"] = {"status": "priced", "currency": "USD", "main": str(main_total),
                             "branch": str(branch_total), "difference": str(branch_total - main_total)}
        except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
            state["cost"] = {"status": "unpriced", "reason": f"Infracost failed: {error}"}
    stack.save(state)
    if state.get("plan", {}).get("resources") is not None:
        write_plan_check(state)
    print(f"Cost: {cost_line(state['cost'])}")


def apply(args):
    state = stack.load()
    result = stack.terraform("apply", "-input=false", "-no-color", str(PLAN_FILE), data_dir=PLAN_DATA,
                             profile=stack.PROFILES["apply"], capture=True, check=False)
    output = result.stdout + result.stderr
    (stack.WORK / "reports" / "apply.txt").write_text(output)
    if result.returncode != 0:
        if "Saved plan is stale" in output:
            print(STALE_MESSAGE, file=sys.stderr)
            sys.exit(STALE)
        print(stack.scrub(output[-5000:]), file=sys.stderr)
        fail("terraform apply failed")
    done = re.search(r"Apply complete! Resources: .*", output)
    state["apply"] = {"summary": done.group(0) if done else "Applied."}
    stack.save(state)
    outputs = stack.terraform("output", "-json", data_dir=PLAN_DATA, profile=stack.PROFILES["apply"], capture=True)
    OUTPUTS.write_text(outputs.stdout)
    print(state["apply"]["summary"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    starter = sub.add_parser("init")
    starter.add_argument("--pr", help="the pull request number, when run by hand")
    stager = sub.add_parser("stage")
    stager.add_argument("name", choices=STAGES)
    stager.add_argument("status", choices=["running", "ok", "failed", "skipped"])
    stager.add_argument("--seconds", type=float)
    sub.add_parser("sync")
    sub.add_parser("gate")
    sub.add_parser("comment")
    changer = sub.add_parser("change")
    changer.add_argument("step", choices=["open", "wait", "implement"])
    changer.add_argument("--timeout", type=int, default=1800)
    changer.add_argument("--interval", type=int, default=15)
    sub.add_parser("has-changes")
    sub.add_parser("verify")
    sub.add_parser("merge")
    finisher = sub.add_parser("finish")
    finisher.add_argument("result", choices=["applied", "no-changes"])
    stopper = sub.add_parser("stopped")
    stopper.add_argument("reason")
    sub.add_parser("plan")
    sub.add_parser("cost")
    sub.add_parser("apply")
    args = parser.parse_args()

    steps = {"init": init, "sync": sync, "stage": stage, "gate": gate, "comment": comment, "change": change_step,
             "has-changes": has_changes, "verify": verify, "merge": merge, "finish": finish, "stopped": stopped, "plan": plan, "cost": cost,
             "apply": apply}
    steps[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
