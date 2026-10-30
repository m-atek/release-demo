"""The release's Jira side, as the user Release Pipeline.

    python scripts/jira.py fix-version            add "<service> <version>" to the issue, creating it if needed
    python scripts/jira.py comment "text"
    python scripts/jira.py release                mark the version released and the issue done
    python scripts/jira.py reopen ORD-12 "orders-service 1.4.0"   for reset.py: back to the start
    python scripts/jira.py issue --type Bug --summary "..." --due 2026-11-13 --label flaky-test --description "..."

A release train (demo 21) runs with several keys, comma-separated: each issue
gets the fix version and the comments, and the version is released once,
without moving any issue, because one of them may ship switched off. issue
creates an issue as Release Pipeline and prints its key (demo 15's
quarantine issues).
"""

import argparse
import base64
import datetime
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import run_state
from common import fail, get_parameter, ssm_client

PROJECT = "ORD"


class Jira:
    def __init__(self):
        ssm = ssm_client()
        self.site = get_parameter(ssm, "jira/site-url").rstrip("/")
        token = base64.b64encode(f"{get_parameter(ssm, 'jira/email')}:{get_parameter(ssm, 'jira/api-token')}".encode())
        self.auth = "Basic " + token.decode()

    def call(self, method, path, body=None):
        request = urllib.request.Request(self.site + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": self.auth, "Accept": "application/json",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            fail(f"Jira {method} {path}: {error.code} {error.read().decode()[:300]}")

    def version(self, name, create=True):
        for version in self.call("GET", f"/rest/api/3/project/{PROJECT}/versions"):
            if version["name"] == name:
                return version
        if not create:
            return None
        project = self.call("GET", f"/rest/api/3/project/{PROJECT}")
        return self.call("POST", "/rest/api/3/version", {"name": name, "projectId": int(project["id"])})

    def create(self, kind, summary, description, labels=(), due=None, fix_version=None):
        """A new issue in ORD, as Release Pipeline, which is its reporter. Returns its key.
        A type the project doesn't have becomes a Task."""
        types = {t["name"]: t["id"] for t in self.call("GET", f"/rest/api/3/issue/createmeta/{PROJECT}/issuetypes")["issueTypes"]}
        fields = {"project": {"key": PROJECT}, "issuetype": {"id": types.get(kind) or types["Task"]},
                  "summary": summary, "description": paragraph(description)}
        if labels:
            fields["labels"] = list(labels)
        if due:
            fields["duedate"] = due
        if fix_version:
            fields["fixVersions"] = [{"id": self.version(fix_version)["id"]}]
        return self.call("POST", "/rest/api/3/issue", {"fields": fields})["key"]

    def search(self, jql, fields="summary,status", limit=50):
        """Issues for a JQL query: [{key, fields}]."""
        query = urllib.parse.urlencode({"jql": jql, "fields": fields, "maxResults": limit})
        return self.call("GET", f"/rest/api/3/search/jql?{query}")["issues"]

    def transition(self, key, target):
        for t in self.call("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]:
            if t["to"]["name"].lower() == target.lower():
                self.call("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                return True
        return False


def paragraph(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.split("\n")]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fix-version")
    commenter = sub.add_parser("comment")
    commenter.add_argument("text")
    sub.add_parser("release")
    creator = sub.add_parser("issue")
    creator.add_argument("--type", default="Task")
    creator.add_argument("--summary", required=True)
    creator.add_argument("--description", default="")
    creator.add_argument("--label", action="append", default=[])
    creator.add_argument("--due", help="the due date, YYYY-MM-DD")
    reopener = sub.add_parser("reopen")
    reopener.add_argument("key")
    reopener.add_argument("version_name")
    args = parser.parse_args()

    jira = Jira()
    if args.command == "reopen":
        version = jira.version(args.version_name, create=False)
        if version and version.get("released"):
            jira.call("PUT", f"/rest/api/3/version/{version['id']}", {"released": False})
        jira.transition(args.key, "Backlog") or jira.transition(args.key, "To Do")
        print(f"{args.key}: reopened; {args.version_name}: {'unreleased' if version else 'not there'}")
        return

    if args.command == "issue":
        print(jira.create(args.type, args.summary, args.description, args.label, args.due))
        return

    state = run_state.load()
    keys, name = state.get("jira_keys") or [state["jira_key"]], f"{state['service']} {state['version']}"
    if args.command == "fix-version":
        version = jira.version(name)
        for key in keys:
            jira.call("PUT", f"/rest/api/3/issue/{key}", {"update": {"fixVersions": [{"add": {"id": version["id"]}}]}})
            print(f"{key}: fix version {name}")
    elif args.command == "comment":
        for key in keys:
            jira.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": paragraph(args.text)})
            print(f"{key}: commented")
    else:
        version = jira.version(name)
        jira.call("PUT", f"/rest/api/3/version/{version['id']}",
                  {"released": True, "releaseDate": datetime.date.today().isoformat()})
        if len(keys) == 1:
            jira.transition(keys[0], "Done")
            print(f"{keys[0]}: done; {name}: released")
        else:
            # A release train ships unfinished work behind a flag: its issues stay as they are.
            print(f"{', '.join(keys)}: as they were; {name}: released")


if __name__ == "__main__":
    sys.exit(main())
