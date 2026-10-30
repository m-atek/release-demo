"""Video 18's one-time setup: its Jira issue and versions, and ServiceNow's cost approver.

    python scripts/demos/cost_gate/setup.py              from the Mac, after the applies and Fiona's password
    python scripts/demos/cost_gate/setup.py --dry-run    what it would make, and nothing else

- Jira, as Release Pipeline: the issue "Show a delivery message for each
  order" in ORD, the next free key, which it prints for the video script's
  ORD-[m]; and the versions "orders-service 1.10.0" and "orders-service 1.10.1"
  on it. Found again by its summary, so a second run makes nothing.
- ServiceNow: servicenow_setup.py --test, which adds Fiona Finance, Cost
  approvers, the field Cost review and the decision at Assess, and proves the
  three routes with test changes it cancels right after.
"""

import argparse
import subprocess
import sys
import urllib.parse

import costs
from common import ROOT

SUMMARY = "Show a delivery message for each order"
VERSIONS = ["1.10.0", "1.10.1"]


def find_issue(jira):
    jql = f'project = ORD AND summary ~ "\\"{SUMMARY}\\"" ORDER BY key'
    found = jira.call("GET", f"/rest/api/3/search/jql?jql={urllib.parse.quote(jql)}&fields=summary&maxResults=10")
    return next((issue["key"] for issue in found["issues"] if issue["fields"]["summary"] == SUMMARY), None)


def jira_issue(dry_run):
    from jira import PROJECT, Jira, paragraph

    jira = Jira()
    key = find_issue(jira)
    if key:
        print(f"Jira: {key} \"{SUMMARY}\" is there")
    elif dry_run:
        print(f"Jira: would create \"{SUMMARY}\" in {PROJECT} with the next free key")
    else:
        types = {t["name"]: t["id"] for t in jira.call("GET", f"/rest/api/3/issue/createmeta/{PROJECT}/issuetypes")["issueTypes"]}
        fields = {"project": {"key": PROJECT}, "issuetype": {"id": types.get("Story") or types["Task"]}, "summary": SUMMARY,
                  "labels": [costs.SERVICE],
                  "description": paragraph("Each order on the order page shows a short delivery message.")}
        key = jira.call("POST", "/rest/api/3/issue", {"fields": fields})["key"]
        print(f"Jira: created {key} \"{SUMMARY}\": the video script's ORD-[m]")
    for version in VERSIONS:
        name = f"{costs.SERVICE} {version}"
        if dry_run:
            print(f"Jira: would add the version {name}{f' to {key}' if key else ''}")
            continue
        found = jira.version(name)
        jira.call("PUT", f"/rest/api/3/issue/{key}", {"update": {"fixVersions": [{"add": {"id": found["id"]}}]}})
        print(f"Jira: {key} carries {name}")
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    jira_issue(args.dry_run)
    if args.dry_run:
        print("ServiceNow: would run scripts/servicenow_setup.py --test")
        return 0
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "servicenow_setup.py"), "--test"], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
