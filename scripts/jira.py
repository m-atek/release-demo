"""The release's Jira side, as the user Release Pipeline.

    python scripts/jira.py fix-version            add "<service> <version>" to the issue, creating it if needed
    python scripts/jira.py comment "text"
    python scripts/jira.py release                mark the version released and the issue done
    python scripts/jira.py reopen ORD-12 "orders-service 1.4.0"   for reset.py: back to the start
"""

import argparse
import base64
import datetime
import json
import sys
import urllib.error
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

    state = run_state.load()
    key, name = state["jira_key"], f"{state['service']} {state['version']}"
    if args.command == "fix-version":
        version = jira.version(name)
        jira.call("PUT", f"/rest/api/3/issue/{key}", {"update": {"fixVersions": [{"add": {"id": version["id"]}}]}})
        print(f"{key}: fix version {name}")
    elif args.command == "comment":
        jira.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": paragraph(args.text)})
        print(f"{key}: commented")
    else:
        version = jira.version(name)
        jira.call("PUT", f"/rest/api/3/version/{version['id']}",
                  {"released": True, "releaseDate": datetime.date.today().isoformat()})
        jira.transition(key, "Done")
        print(f"{key}: done; {name}: released")


if __name__ == "__main__":
    sys.exit(main())
