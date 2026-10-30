"""Demo 23's one-time setup, after the service principal's secret is in SSM and with the endpoint up.

    .venv/bin/python scripts/demos/rag/setup.py                 every step, in order
    .venv/bin/python scripts/demos/rag/setup.py --only jira     one step (sonar, servicenow, jira, changes, release, stage)

- sonar: the SonarQube Cloud project m-atek_release-demo_runbook-assistant;
- servicenow: the CMDB item and the two templates, through servicenow_setup.py;
- jira: two issues in ORD, the next free keys, printed for the video
  script's placeholders: ORD-[m] for 2.0.0 and ORD-[n] for 2.1.x;
- changes: the snapshot of past changes, about 30 closed ones from the
  platform's own ServiceNow history (number, short description, close code,
  close notes, close time), into corpus/changes.json of all three demo
  states, committed to main by the GitHub App: the same in all three, so
  they share a chunk hash;
- release: 2.0.0, for real: staged on main, then a standard release as Sam,
  started the way history.py starts one, on the tools host. It builds the
  pages table, the chunk table and the index, and its evaluation run is the
  comparison's left side;
- stage: 2.1.0 staged on main, ready for the first take.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # scripts/ first: its host.py
sys.path.append(str(HERE))
sys.path.append(str(HERE.parent / "terraform"))  # demo 2's GitHub App client
import host  # noqa: E402
import rag  # noqa: E402
from common import ROOT, fail, get_parameter, service, ssm_client  # noqa: E402

STEPS = ["sonar", "servicenow", "jira", "changes", "release", "stage"]
ISSUES = [("2.0.0", "Answer on-call questions from the runbooks and past changes"),
          ("2.1.x", "Search the runbooks by exact service names")]
SNAPSHOT = 30
URL = re.compile(r"\s*(Build|Jenkins build|Splunk|Grafana): https?://\S+")


def sonar(args):
    ssm = ssm_client()
    organization, token = get_parameter(ssm, "sonar/organization"), get_parameter(ssm, "sonar/token")
    key = service(rag.SERVICE)["sonarqube"]["project_key"]
    query = urllib.parse.urlencode({"organization": organization, "project": key, "name": rag.SERVICE,
                                    "visibility": "public"})
    request = urllib.request.Request(f"https://sonarcloud.io/api/projects/create?{query}", method="POST",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30):
            print(f"  SonarQube Cloud project {key}: created")
    except urllib.error.HTTPError as error:
        text = error.read().decode(errors="replace")
        if error.code == 400 and "already exist" in text.lower():
            print(f"  SonarQube Cloud project {key}: already there")
        else:
            fail(f"SonarQube Cloud refused the project ({error.code}): {text[:200]}")


def servicenow(args):
    import subprocess

    subprocess.run([sys.executable, str(ROOT / "scripts" / "servicenow_setup.py")], check=True)


def jira(args):
    from jira import PROJECT, Jira, paragraph

    client = Jira()
    project = client.call("GET", f"/rest/api/3/project/{PROJECT}")
    found = client.call("POST", "/rest/api/3/search/jql", {"jql": f"project = {PROJECT} AND summary ~ \"runbooks\"",
                                                           "fields": ["summary"], "maxResults": 20})
    existing = {issue["fields"]["summary"]: issue["key"] for issue in (found or {}).get("issues", [])}
    for version, summary in ISSUES:
        if summary in existing:
            print(f"  {existing[summary]}: {summary} ({version}), already there")
            continue
        made = client.call("POST", "/rest/api/3/issue", {"fields": {
            "project": {"id": project["id"]}, "summary": summary, "issuetype": {"name": "Story"},
            "description": paragraph(f"runbook-assistant {version}. Demo 23.")}})
        print(f"  {made['key']}: {summary} ({version}), created")


def snapshot(rows):
    """The changes as the corpus keeps them: no links, closed ones only, oldest first."""
    found = []
    for row in rows:
        notes = URL.sub("", row.get("close_notes") or "").strip()
        found.append({"number": row["number"], "short_description": row["short_description"],
                      "close_code": row.get("close_code") or "", "close_notes": notes,
                      "closed": (row.get("closed_at") or "").replace(" ", "T") + "Z" if row.get("closed_at") else ""})
    return sorted(found, key=lambda c: c["number"])


def wanted_changes(rows, limit=SNAPSHOT):
    """The changes the questions name first, then the newest releases and rollbacks, up to limit."""
    questions, _ = rag.questions()
    named = {s["change"] for q in questions for s in q.get("expected_sources") or [] if isinstance(s, dict)}
    first = [r for r in rows if r["short_description"] in named]
    rest = [r for r in sorted(rows, key=lambda r: r["number"], reverse=True)
            if r not in first and r["short_description"].startswith(("Release ", "Rollback of "))]
    picked = (first + rest)[:max(limit, len(first))]
    missing = sorted(named - {r["short_description"] for r in picked})
    return picked, missing


def changes(args):
    import change
    import github

    sn, _ = change.connect()
    rows = sn.call("GET", "/api/now/table/change_request", sysparm_query="state=3^ORDERBYDESCnumber",
                   sysparm_fields="number,short_description,close_code,close_notes,closed_at",
                   sysparm_display_value="false", sysparm_limit=300) or []
    picked, missing = wanted_changes(rows)
    for description in missing:
        print(f"  no closed change \"{description}\" yet: its question has no source until it exists")
    text = json.dumps(snapshot(picked), indent=2) + "\n"
    gh = github.GitHub()
    files = {f"demo-states/{rag.SERVICE}/{v}/corpus/changes.json": text for v in ("2.0.0", "2.1.0", "2.1.1")}
    files[f"{service(rag.SERVICE)['path']}/corpus/changes.json"] = text
    parent = gh.branch_sha("main")
    if all(gh.file_at(path, "main") == text for path in files):
        print(f"  changes.json: {len(picked)} changes, already on main")
        return
    sha = gh.commit_files(parent, files, f"demo: {rag.SERVICE}'s past changes from ServiceNow [skip ci]")
    gh.set_branch("main", sha)
    print(f"  changes.json: {len(picked)} closed changes, committed {sha[:7]} to main")


def release(args):
    """2.0.0 as a standard release, on the tools host, as history.py releases."""
    if not host.on_host():
        sys.exit(host.hand_over("rag-setup", "demos/rag/setup.py", ["--only", "release"],
                                title="setup.py release runbook-assistant 2.0.0"))
    from history import watch
    from jira import PROJECT, Jira
    from release_job import ReleaseJob

    found = Jira().call("POST", "/rest/api/3/search/jql", {"jql": f"project = {PROJECT} AND summary ~ \"{ISSUES[0][1]}\"",
                                                           "fields": ["summary"], "maxResults": 1})
    if not (found or {}).get("issues"):
        fail("No Jira issue for 2.0.0 yet: setup.py --only jira")
    key = found["issues"][0]["key"]
    host.stage(rag.SERVICE, "2.0.0")
    job = ReleaseJob.as_release_bot()
    queue_id = job.start({"SERVICE": rag.SERVICE, "JIRA_KEY": key, "OPTION": "",
                          "REQUESTED_BY": get_parameter(ssm_client(), "slack/member-id/sam"),
                          "SLACK_CHANNEL": "", "SLACK_TS": ""})
    number = job.started(queue_id, waiting=lambda why: print(f"  queued: {why}"))
    print(f"  release build #{number}: {rag.SERVICE} 2.0.0 for {key}")
    build = watch(job, number, f"{rag.SERVICE} 2.0.0")
    if build["result"] != "SUCCESS":
        fail(f"The 2.0.0 release ended {build['result']}: look at build #{number} in Jenkins.")
    print("  2.0.0 is live in staging and production, with its evaluation run in MLflow")


def stage(args):
    host.stage(rag.SERVICE, "2.1.0")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default=",".join(STEPS), help=f"comma-separated, from: {', '.join(STEPS)}")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    for name in [s for s in STEPS if s in args.only.split(",")]:
        print(name)
        globals()[name](args)
    print("Done. The video script's ORD-[m] and ORD-[n] are the two keys the jira step printed.")


if __name__ == "__main__":
    main()
