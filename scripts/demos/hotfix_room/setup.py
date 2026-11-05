"""Video 33, once: the two Jira issues, forecast's CMDB item and its SonarQube Cloud project.

    python scripts/demos/hotfix_room/setup.py             makes what isn't there, and prints the keys
    python scripts/demos/hotfix_room/setup.py --dry-run   says what it would make, and reads nothing but what is there

ORD-[h] is the hotfix catalog 1.12.1 goes out under, ORD-[r] forecast 2.4.1's
reservation. Each is made once, here or by reset.py hotfix-room. Safe to run again.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parents[1]))  # after the standard library: scripts/ has a secrets.py

from common import get_parameter, service, ssm_client  # noqa: E402
from jira import PROJECT, Jira  # noqa: E402

HOTFIX_SUMMARY = "Supplier search finds nothing in lower case"
HOTFIX_DESCRIPTION = "Searching the catalog by supplier only finds a supplier written with its capitals."
ROOM_SUMMARY = "forecast: give back the memory it reserved and doesn't use"
ROOM_DESCRIPTION = ("forecast reserves far more memory than it uses, for a month-end run that moved off this "
                    "service. Bring its requests down to what it needs.")
ISSUES = [("Bug", HOTFIX_SUMMARY, HOTFIX_DESCRIPTION, "catalog", "ORD-[h]"),
          ("Story", ROOM_SUMMARY, ROOM_DESCRIPTION, "forecast", "ORD-[r]")]
SERVICE = "forecast"


def found_issue(jira, summary):
    """The issue's key if it exists: the search matches words, so the summary is compared whole."""
    for item in jira.search(f'project = {PROJECT} AND summary ~ "\\"{summary}\\"" ORDER BY key', fields="summary"):
        if item["fields"]["summary"] == summary:
            return item["key"]
    return None


def issue(jira, kind, summary, description, label, dry_run):
    key = found_issue(jira, summary)
    if key:
        return key, "there already"
    if dry_run:
        return "ORD-?", "would make it"
    return jira.create(kind, summary, description, labels=[label]), "made now"


def cmdb_item(dry_run):
    """forecast-production, as servicenow_setup.py makes every service's, with the setup user."""
    from servicenow_setup import SETUP_USER, ServiceNow

    ssm = ssm_client()
    sn = ServiceNow(get_parameter(ssm, "servicenow/instance-url"), SETUP_USER, get_parameter(ssm, "servicenow/admin-password"))
    name = service(SERVICE)["servicenow"]["cmdb_item"]
    if dry_run:
        print(f"CMDB item {name}: {'there already' if sn.find('cmdb_ci_appl', f'name={name}', 'sys_id') else 'would make it'}")
        return
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")["sys_id"]
    sn.ensure("cmdb_ci_appl", f"name={name}", {
        "name": name, "short_description": f"{SERVICE}: released by the release demo pipeline. Demo system.",
        "support_group": group, "operational_status": "1"}, f"CMDB item {name}")


def sonar_project(dry_run):
    """The project every release of forecast analyses into, public as the others are."""
    ssm = ssm_client()
    key = service(SERVICE)["sonarqube"]["project_key"]
    token, organization = get_parameter(ssm, "sonar/token"), get_parameter(ssm, "sonar/organization")
    headers = {"Authorization": f"Bearer {token}"}
    query = urllib.parse.urlencode({"organization": organization, "projects": key})
    with urllib.request.urlopen(urllib.request.Request(f"https://sonarcloud.io/api/projects/search?{query}", headers=headers),
                                timeout=30) as response:
        if json.loads(response.read()).get("components"):
            print(f"SonarQube Cloud project {key}: there already")
            return
    if dry_run:
        print(f"SonarQube Cloud project {key}: would make it")
        return
    form = urllib.parse.urlencode({"organization": organization, "project": key, "name": SERVICE, "visibility": "public"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://sonarcloud.io/api/projects/create", data=form, headers=headers),
                                timeout=30):
        print(f"SonarQube Cloud project {key}: made now")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="say what it would make, make nothing")
    args = parser.parse_args()
    jira = Jira()
    for kind, summary, description, label, placeholder in ISSUES:
        key, what = issue(jira, kind, summary, description, label, args.dry_run)
        print(f"{key}: {summary} ({what}). It is {placeholder} in the script.")
    cmdb_item(args.dry_run)
    sonar_project(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
