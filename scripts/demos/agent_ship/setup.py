"""Video 16, once: the Jira issue the coding agent works on, and SonarQube's project for pull requests.

    python scripts/demos/agent_ship/setup.py            the issue and its fix version, as Release Pipeline
    python scripts/demos/agent_ship/setup.py --sonar    also the SonarQube Cloud project the pull request checks analyse into

The issue: a Story, "Show the order count per store", labelled
orders-service, with the Jira version "orders-service 1.9.0" (unreleased) as
its fix version. The spec named 1.6.0, but videos 20 (1.6.x and 1.7.x)
and 21 (1.8.0) release those, and video 18 releases 1.10.x, so it takes the
next free minor version; it also skips any version a demo state or another
Jira issue holds.
It prints the issue's key, which the video's script names ORD-[a]. Safe to
run twice: it finds the issue it made.
"""

import argparse
import sys
import urllib.parse

import agent  # noqa: F401  (puts scripts/ on the path)
from common import ROOT, fail, get_parameter, ssm_client

SUMMARY = "Show the order count per store"
DESCRIPTION = ("On the order page, under today's orders, show how many orders each store has today, busiest first. "
               "Keep /api/orders as it is. Add a test.")
LABEL = "orders-service"
SERVICE = "orders-service"
FIRST_CHOICE = (1, 9, 0)
# Minor versions other videos release: 15 (1.5), 20 (1.6, 1.7), 21 (1.8), 18 (1.10).
CLAIMED = {(1, 5), (1, 6), (1, 7), (1, 8), (1, 10)}


def free_version(jira, ours=None):
    """The first minor version from 1.9.0 that no demo state, no other video and no other issue holds."""
    states = {tuple(int(p) for p in d.name.split(".")[:2]) for d in (ROOT / "demo-states" / SERVICE).iterdir()
              if d.is_dir() and d.name[0].isdigit()}
    major, minor, _ = FIRST_CHOICE
    while True:
        name = f"{SERVICE} {major}.{minor}.0"
        others = search(jira, f'fixVersion = "{name}"' + (f" AND key != {ours}" if ours else ""))
        if (major, minor) not in states | CLAIMED and not others:
            return name
        minor += 1


def search(jira, jql):
    query = urllib.parse.quote(f"project = ORD AND {jql}")
    try:
        return jira.call("GET", f"/rest/api/3/search/jql?jql={query}&fields=summary,fixVersions,status&maxResults=20")["issues"]
    except SystemExit:
        return []


def find_issue(jira):
    found = search(jira, f'summary ~ "\\"{SUMMARY}\\"" AND labels = {LABEL} ORDER BY key')
    return next((i for i in found if i["fields"]["summary"] == SUMMARY), None)


def setup_issue(jira):
    from jira import paragraph

    existing = find_issue(jira)
    if existing:
        versions = ", ".join(v["name"] for v in existing["fields"]["fixVersions"]) or "none"
        print(f"{existing['key']}: \"{SUMMARY}\", fix version {versions}, {existing['fields']['status']['name']} (made earlier)")
        return existing["key"]
    name = free_version(jira)
    version = jira.version(name)
    project = jira.call("GET", "/rest/api/3/project/ORD")
    created = jira.call("POST", "/rest/api/3/issue", {"fields": {
        "project": {"id": project["id"]}, "issuetype": {"name": "Story"}, "summary": SUMMARY,
        "description": paragraph(DESCRIPTION), "labels": [LABEL], "fixVersions": [{"id": version["id"]}]}})
    print(f"{created['key']}: \"{SUMMARY}\", fix version {name}. The video's ORD-[a] is {created['key']}.")
    return created["key"]


def setup_sonar(ssm, key):
    """The pull request checks' own project: SonarQube Cloud's Free plan analyses only main."""
    import json
    import urllib.request

    token, organization = get_parameter(ssm, "sonar/token"), get_parameter(ssm, "sonar/organization")
    headers = {"Authorization": f"Bearer {token}"}
    search_url = f"https://sonarcloud.io/api/projects/search?organization={organization}&projects={key}"
    with urllib.request.urlopen(urllib.request.Request(search_url, headers=headers), timeout=30) as response:
        if json.loads(response.read()).get("components"):
            print(f"SonarQube Cloud project {key}: there")
            return
    form = urllib.parse.urlencode({"organization": organization, "project": key, "name": "orders-service pull requests",
                                   "visibility": "public"}).encode()
    request = urllib.request.Request("https://sonarcloud.io/api/projects/create", data=form, headers=headers)
    with urllib.request.urlopen(request, timeout=30):
        print(f"SonarQube Cloud project {key}: created, public, analysed by the pull request checks only")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sonar", action="store_true", help="also the SonarQube Cloud project for pull requests")
    args = parser.parse_args()
    from jira import Jira

    from common import service

    setup_issue(Jira())
    if args.sonar:
        key = service(SERVICE)["pull_requests"].get("sonar_project_key")
        if not key:
            fail("services.yaml names no pull_requests.sonar_project_key: the checks use SonarQube's own pull request analysis.")
        setup_sonar(ssm_client(), key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
