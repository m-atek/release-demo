"""Who wrote a release's code and who reviewed it, from GitHub, for the change (video 16).

For a service with pull_requests in services.yaml, change.py open lists the
release's commits from the one production runs (as its /health reports it)
to this one, and the merged pull requests they belong to, through the
pipeline's GitHub App. For each: its author and its approving reviews on
the merged head, mapped to people through people.yaml, where a login two
entries share (yours, which plays Sam on GitHub) maps to the releaser.

The rule: a pull request written by an entry with agent: true needs an
approving review by a person who isn't an agent and isn't its author.
Without one, the change doesn't open. Releases of stage.py commits have no
pull request and are unchanged, and so is every service without the key.
"""

import json
import re

import yaml

from common import ROOT, github_api, github_app_token, http_get, service, ssm_client

SHA = re.compile(r"[0-9a-f]{40}")


def everyone():
    return yaml.safe_load((ROOT / "people.yaml").read_text())


def person(login, people):
    """(key, entry) for a GitHub login; a shared login maps to the releaser first."""
    matches = sorted(((key, p) for key, p in people.items() if p.get("github_login") == login),
                     key=lambda item: not item[1].get("releaser"))
    return matches[0] if matches else (None, {"display_name": login})


def production_commit(name):
    """The commit production's /health reports, or None (a service off Lambda, or one never built from a commit)."""
    from deploy import lambda_client

    function = service(name)["environments"]["production"]["function"]
    url = lambda_client(name).get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"]
    status, body = http_get(url.rstrip("/") + "/health", timeout=15)
    commit = json.loads(body).get("commit", "") if status == 200 else ""
    return commit if SHA.fullmatch(commit) else None


class GitHub:
    def __init__(self, token=None, repository=None):
        if token is None:
            token, repository, _ = github_app_token(ssm_client())
        self.token, self.repository = token, repository

    def get(self, path):
        return github_api("GET", f"/repos/{self.repository}{path}", self.token, scheme="token")

    def commits(self, base, head):
        if not base:
            return [head]
        return [c["sha"] for c in self.get(f"/compare/{base}...{head}").get("commits", [])]

    def pulls_of(self, sha):
        return [p for p in self.get(f"/commits/{sha}/pulls") or []
                if p.get("merged_at") and p.get("base", {}).get("ref") == "main"]

    def approvals(self, pull):
        """Approving reviews on the pull request's merged head: a later push dismisses earlier ones."""
        head = pull["head"]["sha"]
        return [r for r in self.get(f"/pulls/{pull['number']}/reviews?per_page=100") or []
                if r.get("state") == "APPROVED" and r.get("commit_id") == head]


def review(github, base, head, people=None):
    """What the change records about the release's pull requests:
    {"pulls": [{"number", "title", "author", "agent", "reviewers"}], "refused": [lines]}."""
    people = people or everyone()
    pulls, seen = [], set()
    for sha in github.commits(base, head):
        for pull in github.pulls_of(sha):
            if pull["number"] in seen:
                continue
            seen.add(pull["number"])
            author_key, author = person(pull["user"]["login"], people)
            reviewers = []
            for approval in github.approvals(pull):
                key, entry = person(approval["user"]["login"], people)
                if entry.get("agent") or key == author_key or approval["user"]["login"] == pull["user"]["login"]:
                    continue
                if entry["display_name"] not in reviewers:
                    reviewers.append(entry["display_name"])
            pulls.append({"number": pull["number"], "title": pull["title"], "author": author["display_name"],
                          "agent": bool(author.get("agent")), "reviewers": reviewers})
    refused = [f"Pull request #{p['number']} was written by the {p['author'][0].lower() + p['author'][1:]} and has "
               "no approving review by a person." for p in pulls if p["agent"] and not p["reviewers"]]
    return {"pulls": sorted(pulls, key=lambda p: p["number"]), "refused": refused}


def record(found):
    """The change's two fields and its description line, or nothing without pull requests."""
    pulls = found["pulls"]
    if not pulls:
        return {}, []
    written = ", ".join(f"{p['author']} (pull request #{p['number']})" for p in pulls)
    reviewed = ", ".join(dict.fromkeys(name for p in pulls for name in p["reviewers"])) or "nobody"
    lines = [f"Pull request #{p['number']}: written by {p['author']}, approved on GitHub by "
             f"{', '.join(p['reviewers']) or 'nobody'}." for p in pulls]
    return {"u_code_written_by": written, "u_code_reviewed_by": reviewed}, lines


def for_release(state, github=None):
    """None for a service without pull_requests: change.py does exactly what it did before."""
    if not service(state["service"]).get("pull_requests"):
        return None
    return review(github or GitHub(), production_commit(state["service"]), state["commit"])
