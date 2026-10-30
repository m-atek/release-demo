"""The pipeline for pull requests to catalog's production on the cluster host,
one step per call, in demo 2's pr.py pattern: one Slack message per pull
request, rewritten at every step, GitHub check runs, an emergency change
waited on like a release's, and the merge through the GitHub App.

On the tools-host agent:
    fix.py scope                         exit 1 for a pull request that changes nothing in deploy/production/catalog/,
                                         2 when GitHub can't say
    fix.py init [--pr 7]                 run state from the pull request and its incident, and the Slack message
    fix.py files                         check: nothing changed outside deploy/production/catalog/
    fix.py digest                        check: JFrog holds the image the values pin
    fix.py stage Change running|ok|failed [--seconds 12.3]
    fix.py gate                          stop when a check failed; Slack and GitHub name it
    fix.py change open|wait|implement    the emergency change, linked to the incident, and Alex's approval
    fix.py merge                         squash, only if the head is the commit the checks ran on
    fix.py sync                          Argo CD Synced at the merge commit and Healthy, within 3 minutes
    fix.py finish                        close the change, resolve the incident, "Live in ..."
    fix.py stopped "reason"
In the gitops toolchain image (Helm 4 and kubeconform):
    fix.py render                        check: helm template with the pull request's values
    fix.py validate                      check: kubeconform against the cluster's Kubernetes version

The requester is the editor the incident recorded (Pieter On Call), not the
pull request's GitHub author, which is Mateusz's account. The total counts
from the pull request's creation on GitHub, so "Live in" matches a clock
started on gh pr create. The run state is .release/state.json, as for a release.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
sys.path.append(str(HERE.parent / "terraform"))  # demo 2's GitHub App client
import gitops  # noqa: E402
import github  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, fail, get_parameter, people, service, ssm_client  # noqa: E402

STAGES = ["Checks", "Change", "Approval", "Merge", "Sync", "Record"]
CHECKS = ["helm template", "kubeconform", "Image in JFrog", "Only deploy/production/catalog"]
WORK = run_state.PATH.parent
RENDERED = WORK / "rendered.yaml"
NOT_FOR_US = "Not for this pipeline."
AUTHORIZE = "-3"


def load():
    return run_state.load()


def save(state):
    run_state.save(state)


def checks_dir():
    return WORK / "checks"


def slug(name):
    return name.lower().replace(" ", "-").replace("/", "-")


def record(name, ok, title, summary, text="", failures=()):
    """One check's result, which the Jenkinsfile publishes as a GitHub check run."""
    result = {"name": name, "conclusion": "success" if ok else "failure", "title": title[:120],
              "summary": summary, "text": text[:60000], "failures": list(failures)}
    checks_dir().mkdir(parents=True, exist_ok=True)
    (checks_dir() / f"{slug(name)}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {title}")
    return result


def results():
    found = {}
    for name in CHECKS:
        path = checks_dir() / f"{slug(name)}.json"
        if path.exists():
            found[name] = json.loads(path.read_text())
    return found


# Slack: one message per pull request, rewritten at every step.

def render(state):
    pr = state["pull_request"]
    incident = f" · {state['incident']['number']}" if state.get("incident") else ""
    head = (f"*Change to catalog production* · <{pr['url']}|pull request #{pr['number']}>{incident} · "
            f"requested by {state['requester_name']}")
    rows = []
    for name in STAGES:
        stage = state["stages"][name]
        took = f"  _{run_state.duration(stage['seconds'])}_" if stage.get("seconds") else ""
        detail = f"  ·  {stage['detail']}" if stage.get("detail") else ""
        rows.append(f"{icon(stage['status'])}  {name}{took}{detail}")
        if name == "Checks":
            for check, found in sorted(state.get("checks", {}).items()):
                if found.get("conclusion") == "failure":
                    rows.append(f"{icon('failed')}  *{check}*: {found['title']}")
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


def icon(status):
    import notify

    return notify.ICON[status]


def post(state, ssm=None):
    import notify

    ssm = ssm or ssm_client()
    token = get_parameter(ssm, "slack/bot-token")
    if state["slack"].get("ts"):
        notify.slack("chat.update", token, channel=state["slack"]["channel"], ts=state["slack"]["ts"], text=render(state))
        return
    reply = notify.slack("chat.postMessage", token, channel=get_parameter(ssm, "slack/channel-id"), text=render(state),
                         unfurl_links=False)
    if reply.get("ok"):
        state["slack"] = {"channel": reply["channel"], "ts": reply["ts"]}
        save(state)


def send_event(ssm, state, stage, result, seconds=None, deploy=False):
    import events

    body = {"build": state["build"]["number"], "build_url": state["build"].get("url"), "service": gitops.SERVICE,
            "version": state["version"], "environment": "production" if deploy else None,
            "change": state.get("change", {}).get("number"), "jira_key": state.get("incident", {}).get("number"),
            "stage": stage, "result": result, "duration_seconds": seconds, "requested_by": state.get("requested_by"),
            "option": "gitops", "pull_request": state["pull_request"]["number"]}
    if deploy:
        body["deploy"] = True
        body["commit_time"] = state.get("commit_time")
        if state.get("commit_time"):
            body["lead_time_seconds"] = round(time.time() - state["commit_time"])
    print(f"event {stage} {result}: Splunk {events.to_splunk(ssm, body)}, Grafana {events.to_loki(ssm, body)}")


def note(state, text):
    if state.get("change"):
        import change as servicenow_change

        sn, _ = servicenow_change.connect()
        servicenow_change.note(sn, state, text)


# The pull request and its incident.

def epoch(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def pull_files(gh, number):
    return gh.call("GET", f"/pulls/{number}/files?per_page=100") or []


def scope(args):
    """Exit 1, at once and without a word to Slack, for a pull request this pipeline isn't for;
    2 when GitHub can't say. scripts/pr_scope.py, which demo 2's job uses too."""
    import pr_scope

    sys.argv = ["pr_scope.py", gitops.settings()["path"], *(["--pr", args.pr] if args.pr else [])]
    sys.exit(pr_scope.main())


def requester(incident, pull, head):
    """(people.yaml key, display name, ServiceNow user) of who asked: the incident's
    recorded editor, else the pull request's author as people.yaml maps it."""
    if incident:
        key, person = gitops.person_for(gitops.editor(incident.get("description")))
        if person:
            return key, person["display_name"], person["servicenow_user"]
    key, person = github.requester(pull, head)
    return key or "", person["display_name"], person.get("servicenow_user")


def init(args):
    import change as servicenow_change

    number = args.pr or os.environ.get("CHANGE_ID")
    if not number:
        fail("No pull request: this runs in the job catalog-production, or by hand with --pr <number>.")
    ssm = ssm_client()
    gh = github.GitHub(ssm)
    pull = gh.pull(number)
    head = gh.commit(pull["head"]["sha"])
    path = gitops.values_path()
    changes = gitops.values_diff(gh.file_at(path, "main"), gh.file_at(path, pull["head"]["sha"]))
    incident_number = gitops.incident_in(pull["title"])
    sn, _ = servicenow_change.connect()
    incident = (sn.find("incident", f"number={incident_number}", "sys_id,number,description,state")
                if incident_number else None)
    key, name, user = requester(incident, pull, head)
    state = {
        "service": gitops.SERVICE,
        "kind": "gitops-pull-request",
        "version": f"PR #{number} {pull['head']['sha'][:7]}",
        "pull_request": {"number": int(number), "title": pull["title"], "url": pull["html_url"],
                         "head_sha": pull["head"]["sha"], "branch": pull["head"]["ref"],
                         "created_at": pull["created_at"]},
        "commit": pull["head"]["sha"],
        "commit_time": int(epoch(head["commit"]["committer"]["date"])),
        "incident": {"number": incident["number"], "sys_id": incident["sys_id"]} if incident else {},
        "requested_by": key, "requester_name": name, "requester_user": user,
        "changes": changes,
        "build": {"number": os.environ.get("BUILD_NUMBER", "local"), "url": os.environ.get("BUILD_URL", "")},
        "slack": {"channel": "", "ts": ""},
        # From the pull request's creation, so "Live in" matches a clock started at gh pr create.
        "started_at": epoch(pull["created_at"]),
        "stages": {stage: {"status": "waiting"} for stage in STAGES},
        "checks": {},
        "lines": [],
    }
    save(state)
    (WORK / "reports").mkdir(parents=True, exist_ok=True)
    (WORK / "values.yaml").write_text(gh.file_at(path, pull["head"]["sha"]) or "")
    post(state, ssm)
    if incident_number and not incident:
        print(f"{incident_number} isn't in ServiceNow: the change will name no incident.")
    print(f"Pull request #{number}: {pull['title']}, head {pull['head']['sha'][:7]}, asked by {name}; "
          f"{gitops.diff_text(changes)}")


# The checks.

def files(args):
    state = load()
    found = pull_files(github.GitHub(), state["pull_request"]["number"])
    path = gitops.settings()["path"] + "/"
    outside = sorted(f["filename"] for f in found if not f["filename"].startswith(path))
    if outside:
        record("Only deploy/production/catalog", False, f"{len(outside)} file(s) outside {path}",
               f"This pipeline changes only {path}. Open another pull request for the rest.",
               "\n".join(f"- `{name}`" for name in outside), failures=outside[:5])
    else:
        names = ", ".join(f"`{f['filename']}`" for f in found)
        record("Only deploy/production/catalog", True, f"{len(found)} file(s), all in {path}", f"Changed: {names}.")


def digest(args):
    values = yaml.safe_load((WORK / "values.yaml").read_text()) or {}
    ok, title = gitops.image_in_jfrog(values, get_parameter(ssm_client(), "jcr/token"))
    record("Image in JFrog", ok, title, "The image deploy/production/catalog/values.yaml pins, by digest, must be in "
           "JFrog Container Registry's docker-local, where the cluster pulls from.", failures=[] if ok else [title])


def chart_dir():
    return ROOT / gitops.settings()["path"]


def render_chart(args):
    """helm template of the chart with the pull request's values (the checkout is the pull request's head)."""
    helm = os.environ.get("HELM", "helm")
    result = subprocess.run([helm, "template", "catalog", str(chart_dir()), "--namespace", "production"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        record("helm template", False, "The chart doesn't render", "helm template failed.",
               f"```\n{(result.stderr or result.stdout)[-3000:]}\n```", failures=["helm template failed"])
        fail("helm template failed")
    RENDERED.write_text(result.stdout)
    kinds = [d["kind"] for d in yaml.safe_load_all(result.stdout) if d]
    record("helm template", True, f"Renders: {', '.join(kinds)}", "helm template with the pull request's values.")


def validate(args):
    if not RENDERED.exists():
        record("kubeconform", False, "Nothing rendered", "helm template failed first.", failures=["nothing to check"])
        fail("nothing rendered")
    version = gitops.settings()["kubernetes_version"]
    result = subprocess.run(["kubeconform", "-strict", "-summary", "-output", "json", "-kubernetes-version", version,
                             str(RENDERED)], capture_output=True, text=True)
    try:
        report = json.loads(result.stdout or "{}")
    except ValueError:
        report = {}
    problems = [f"{r.get('kind')} {r.get('name')}: {r.get('msg')}" for r in report.get("resources", [])
                if r.get("status") in ("statusInvalid", "statusError")]
    summary = report.get("summary", {})
    if result.returncode == 0 and not problems:
        record("kubeconform", True, f"Valid for Kubernetes {version}: {summary.get('valid', '?')} resources",
               f"kubeconform -strict against Kubernetes {version}, the cluster host's k3s.")
        return
    record("kubeconform", False, f"{len(problems) or 'Some'} invalid for Kubernetes {version}",
           "kubeconform found:", "\n".join(f"- {p}" for p in problems) or result.stderr[-2000:],
           failures=problems[:5] or ["kubeconform failed"])
    fail("kubeconform failed")


def stage(args):
    ssm = ssm_client()
    state = load()
    state["checks"] = {k: {f: v[f] for f in ("conclusion", "title", "failures")} for k, v in results().items()}
    current = state["stages"][args.name]
    current["status"] = args.status
    if args.status == "running":
        current["started"] = time.time()
    elif args.seconds is None and current.get("started"):
        args.seconds = round(time.time() - current["started"], 1)
    if args.seconds is not None:
        current["seconds"] = args.seconds
    if args.name == "Change" and state.get("change"):
        current["detail"] = f"{state['change']['number']} emergency"
    if args.name == "Sync" and state.get("sync"):
        current["detail"] = f"Synced at {state['sync']['revision'][:7]}"
    save(state)
    post(state, ssm)
    if args.status in ("ok", "failed", "skipped"):
        send_event(ssm, state, args.name, args.status, args.seconds, deploy=args.name == "Sync" and args.status == "ok")
        took = f" in {args.seconds:.1f}s" if args.seconds is not None else ""
        note(state, f"{args.name}: {args.status}{took}" + (f". {current['detail']}" if current.get("detail") else ""))


def gate(args):
    ssm = ssm_client()
    state = load()
    found = results()
    state["checks"] = {k: {f: v[f] for f in ("conclusion", "title", "failures")} for k, v in found.items()}
    failed = [name for name in CHECKS if found.get(name, {}).get("conclusion") != "success"]
    state["stages"]["Checks"]["status"] = "failed" if failed else "ok"
    if not failed:
        save(state)
        send_event(ssm, state, "Checks", "ok")
        print("Every check passed.")
        return
    names = " and ".join([", ".join(failed[:-1]), failed[-1]] if len(failed) > 1 else failed)
    state["lines"].append(f"*Blocked*: {names} failed. Nothing reaches production until "
                          f"{'they pass' if len(failed) > 1 else 'it passes'}.")
    state["result"] = "blocked"
    save(state)
    post(state, ssm)
    send_event(ssm, state, "Checks", "failed")
    fail(f"Checks failed: {', '.join(failed)}")


# The emergency change.

def change_fields(state, ids, jira=None):
    """The emergency change's fields: what the pull request changes, the incident, the checks."""
    pr, changes = state["pull_request"], state["changes"]
    incident = state.get("incident", {}).get("number")
    check_lines = "\n".join(f"{'Passed' if c['conclusion'] == 'success' else 'FAILED'}: {name}: {c['title']}"
                            for name, c in sorted(state.get("checks", {}).items()))
    diff = "\n".join(f"{c['key']}: {c['from']} to {c['to']} ({c['path']})" for c in changes) or "No values change."
    started = time.time()
    return {
        "short_description": f"{pr['title']} (#{pr['number']})",
        "description": (f"Change to catalog in production on the cluster host, from pull request #{pr['number']} to "
                        f"deploy/production/catalog.\n\n{diff}\n\n"
                        + (f"Incident: {incident}, a hand edit Argo CD put back. This change makes it in Git.\n"
                           if incident else "")
                        + f"Pull request: {pr['url']}"),
        "cmdb_ci": ids["ci"], "assignment_group": ids["group"], "requested_by": ids["requester"],
        "category": "Software",
        "justification": (f"{incident}: the change the on-call engineer made by hand, now through Git."
                          if incident else f"Pull request #{pr['number']}."),
        "implementation_plan": (f"Jenkins merges pull request #{pr['number']} through the GitHub App, only if its head "
                                "is still the commit the checks ran on. Argo CD then syncs catalog-production from "
                                f"main; Jenkins waits until it is Synced at the merge commit and Healthy.\n"
                                f"Build: {state['build'].get('url', '')}"),
        "backout_plan": "Revert this pull request; Argo CD syncs the previous values from Git.",
        "test_plan": f"Checks on the pull request:\n{check_lines}",
        "risk_impact_analysis": f"{gitops.diff_text(changes)}. Production keeps serving during the rollout.",
        "start_date": run_state.iso(started).replace("T", " ").rstrip("Z"),
        "end_date": run_state.iso(started + 1800).replace("T", " ").rstrip("Z"),
    }


def change_open(state):
    import change as servicenow_change

    sn, _ = servicenow_change.connect()
    config = service(gitops.SERVICE)["servicenow"]
    ci = sn.find("cmdb_ci_appl", f"name={config['cmdb_item']}", "sys_id")
    if not ci:
        fail(f"No CMDB item {config['cmdb_item']} in ServiceNow yet: python scripts/servicenow_setup.py")
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")
    asked = sn.find("sys_user", f"user_name={state.get('requester_user') or 'release.pipeline'}", "sys_id")
    asked = asked or sn.find("sys_user", "user_name=release.pipeline", "sys_id")
    fields = change_fields(state, {"ci": ci["sys_id"], "group": group["sys_id"], "requester": asked["sys_id"]})
    reply = sn.call("POST", "/api/sn_chg_rest/change/emergency", fields)
    change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
    state["change"] = {"sys_id": change_id, "number": number, "type": "emergency",
                       "url": f"{sn.url}/change_request.do?sys_id={change_id}", "requested_by": asked["sys_id"]}
    save(state)
    evidence = WORK / "reports"
    (evidence / "checks.txt").write_text(fields["test_plan"] + "\n")
    (evidence / "values.yaml").write_text((WORK / "values.yaml").read_text())
    for name in ("checks.txt", "values.yaml"):
        servicenow_change.attach(sn, change_id, evidence / name)
    if state.get("incident"):
        # The incident's Change Request field names the change; the change lists it as fixed by it.
        sn.call("PATCH", f"/api/now/table/incident/{state['incident']['sys_id']}",
                {"rfc": change_id, "work_notes": f"Fix through Git: {number}, from pull request "
                                                 f"#{state['pull_request']['number']}."})
    servicenow_change.note(sn, state, f"Evidence:\nPull request: {state['pull_request']['url']}\n"
                                      f"Build: {state['build'].get('url', '')}\nAttached: checks.txt, values.yaml")
    sn.call("PATCH", f"/api/sn_chg_rest/change/emergency/{change_id}", {"state": AUTHORIZE})
    print(f"{number}: emergency, waiting for Change approvers at Authorize")


def change_step(args):
    import change as servicenow_change

    ssm = ssm_client()
    state = load()
    if args.step == "open":
        change_open(state)
        state = load()
        post(state, ssm)
    elif args.step == "wait":
        sn, _ = servicenow_change.connect()
        servicenow_change.wait(sn, state, args.timeout, args.interval)
        state = load()
        state["lines"].append(f"Approved by {state['change']['approved_by']} in ServiceNow.")
        save(state)
        post(state, ssm)
    else:
        sn, _ = servicenow_change.connect()
        servicenow_change.implement(sn, state)


def merge(args):
    state = load()
    gh = github.GitHub()
    pr = state["pull_request"]
    current = gh.pull(pr["number"])
    if current["state"] != "open" or current["head"]["sha"] != pr["head_sha"]:
        fail(f"Pull request #{pr['number']} changed after its checks ran: not merged. Push again for a new run.")
    approver = state["change"].get("approved_by", "Change approvers")
    merged = gh.merge(pr["number"], pr["head_sha"], f"{pr['title']} (#{pr['number']})",
                      f"Merged by Jenkins build #{state['build']['number']} after {approver} approved "
                      f"{state['change']['number']} in ServiceNow. {gitops.diff_text(state['changes'])}.")
    state["merge_sha"] = merged["sha"]
    save(state)
    print(f"Merged pull request #{pr['number']} as {merged['sha'][:7]}")


def sync(args):
    import argocd

    state = load()
    name, timeout = gitops.settings()["application"], gitops.settings()["sync_timeout_seconds"]
    shown = []

    def seen(found):
        line = f"{found['sync']} at {found['revision'][:7] or '?'}, {found['health']}"
        if not shown or shown[-1] != line:
            print(f"  {time.strftime('%H:%M:%S')} {line}", flush=True)
            shown.append(line)

    app = argocd.ArgoCD().wait(name, state["merge_sha"], timeout, seen=seen)
    found = argocd.summary(app)
    state["sync"] = {"revision": found["revision"], "finished_at": found["finished_at"] or run_state.iso(time.time())}
    save(state)


def clock_time(text):
    sys.path.insert(0, str(ROOT / "bridge"))
    import events as bridge_events

    return bridge_events.clock(text)


def finish(args):
    import change as servicenow_change
    import events

    ssm = ssm_client()
    state = load()
    total = run_state.duration(time.time() - state["started_at"])
    state["total"] = total
    synced = clock_time(state["sync"]["finished_at"])
    revision = state["sync"]["revision"][:7]
    sn, _ = servicenow_change.connect()
    servicenow_change.close(sn, state, "successful",
                            f"Synced by Argo CD at {synced}, revision {revision}. Total time: {total}.")
    if state.get("incident"):
        approver = state["change"].get("approved_by_name") or approver_name(sn, state)
        gitops.resolve(sn, state["incident"]["sys_id"],
                       f"Fixed through {state['change']['number']}: {gitops.short_diff(state['changes'])}, approved by "
                       f"{approver}, synced by Argo CD at {synced}.")
    state["lines"].append(f"Live in {total}.")
    state["result"] = "live"
    save(state)
    post(state, ssm)
    events.annotate(ssm, f"catalog in production: {state['pull_request']['title']} synced by Argo CD "
                         f"({state['change']['number']})", ["release", gitops.SERVICE])
    github.GitHub().call("POST", f"/issues/{state['pull_request']['number']}/comments", {
        "body": f"Synced by Argo CD at {synced}, revision {revision}, after {state['change']['number']} was approved "
                f"in ServiceNow. Total time: {total}."})


def approver_name(sn, state):
    """The approver's full name, as ServiceNow records it: Alex Change Manager."""
    rows = sn.call("GET", "/api/now/table/sysapproval_approver",
                   sysparm_query=f"sysapproval={state['change']['sys_id']}^state=approved",
                   sysparm_fields="approver", sysparm_display_value="true", sysparm_limit=1) or []
    return rows[0]["approver"]["display_value"] if rows else state["change"].get("approved_by", "Change approvers")


def stopped(args):
    """Any failure: Slack says where it stopped. Before the merge the change is cancelled and
    the pull request closed with a comment; the incident stays open, nothing reached production."""
    import change as servicenow_change

    ssm = ssm_client()
    state = load()
    if not state or state.get("result"):
        return  # the gate or the finish already said so
    reason = args.reason.strip().splitlines()[0] if args.reason.strip() else "the build failed"
    state["result"] = "stopped"
    state["total"] = run_state.duration(time.time() - state["started_at"])
    merged = bool(state.get("merge_sha"))
    state["lines"].append(f"Stopped after {state['total']}, after the merge: {reason}" if merged
                          else f"Stopped after {state['total']}. Nothing reached production.")
    save(state)
    post(state, ssm)
    send_event(ssm, state, "Pipeline", "failed")
    if state.get("change"):
        sn, _ = servicenow_change.connect()
        current = sn.call("GET", f"/api/now/table/change_request/{state['change']['sys_id']}",
                          sysparm_fields="state")["state"]
        if current in (servicenow_change.CLOSED, servicenow_change.CANCELED):
            print(f"{state['change']['number']} is closed already")  # a rejection cancels it in change.py wait
        elif merged:
            servicenow_change.close(sn, state, "unsuccessful", f"Stopped after the merge: {reason}")
        else:
            servicenow_change.cancel(sn, state, f"Stopped before the merge: {reason} Nothing changed in production.")
    if not merged:
        github.GitHub().close_pull(state["pull_request"]["number"],
                                   f"Stopped: {reason} Nothing was merged and production is unchanged. "
                                   "The incident stays open.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scope", "init"):
        sub.add_parser(name).add_argument("--pr", help="the pull request number, when run by hand")
    for name in ("files", "digest", "render", "validate", "gate", "merge", "sync", "finish"):
        sub.add_parser(name)
    stager = sub.add_parser("stage")
    stager.add_argument("name", choices=STAGES)
    stager.add_argument("status", choices=["running", "ok", "failed", "skipped"])
    stager.add_argument("--seconds", type=float)
    changer = sub.add_parser("change")
    changer.add_argument("step", choices=["open", "wait", "implement"])
    changer.add_argument("--timeout", type=int, default=1800)
    changer.add_argument("--interval", type=int, default=15)
    stopper = sub.add_parser("stopped")
    stopper.add_argument("reason")
    args = parser.parse_args()
    steps = {"scope": scope, "init": init, "files": files, "digest": digest, "render": render_chart,
             "validate": validate, "stage": stage, "gate": gate, "change": change_step, "merge": merge,
             "sync": sync, "finish": finish, "stopped": stopped}
    steps[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
