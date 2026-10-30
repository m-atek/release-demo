"""The take's pull request, "ORD-12 Show the delivery estimate on the order
page", made with your own gh login, so GitHub shows it as yours.

    python scripts/demos/github_actions/pr.py open     or: scripts/demo.sh open-pr github-actions
    python scripts/demos/github_actions/pr.py close    close the open one; merged ones stay
    python scripts/demos/github_actions/pr.py status

open makes a branch from main with demo-states/orders-service/1.4.0 in
services/orders-service, one commit whose message is the title, and opens the
pull request; you merge it with "Squash and merge", and its subject, with the
Jira key in front, starts release.yml. A branch name an earlier take used gets
the next number (demo-14/delivery-estimate-2), so nothing is deleted or
force-pushed. Main must be on 1.3.0: reset.py github-actions puts it there.
"""

import argparse
import base64
import sys

from github_demo import BASELINE, BRANCH, ROOT, SERVICE, TITLE, VERSION, fail, gh_api, gh_signed_in, repository

AUTHOR = {"name": "Mateusz Ulas", "email": "noreply@es.nl"}
BODY = ("Shows the delivery estimate on the order page: \"Delivery estimate: 2 days\".\n\n"
        "ORD-12. Merging it releases orders-service 1.4.0 from GitHub Actions; production waits for the change's "
        "approval in ServiceNow.")


def open_pulls(repo):
    pulls = gh_api("GET", f"/repos/{repo}/pulls?state=open&per_page=50") or []
    return [p for p in pulls if p["title"] == TITLE and p["head"]["ref"].startswith("demo-14/")]


def free_branch(repo):
    taken = {ref["ref"].removeprefix("refs/heads/")
             for ref in gh_api("GET", f"/repos/{repo}/git/matching-refs/heads/{BRANCH}") or []}
    if BRANCH not in taken:
        return BRANCH
    number = 2
    while f"{BRANCH}-{number}" in taken:
        number += 1
    return f"{BRANCH}-{number}"


def state_files():
    """demo-states/orders-service/1.4.0, as the paths it gets in services/orders-service."""
    source = ROOT / "demo-states" / SERVICE / VERSION
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_file() and "__pycache__" not in relative.parts and ".pytest_cache" not in relative.parts:
            yield f"services/{SERVICE}/{relative.as_posix()}", path.read_text()


def tree_entries(main_files, files):
    """The tree's changes: every file of the state, and the service's other files on main deleted."""
    entries = [{"path": path, "mode": "100644", "type": "blob", "content": content} for path, content in files]
    wanted = {entry["path"] for entry in entries}
    entries += [{"path": path, "mode": "100644", "type": "blob", "sha": None}
                for path in sorted(main_files) if path not in wanted]
    return entries


def open_pull(repo):
    existing = open_pulls(repo)
    if existing:
        print(f"Already open: {existing[0]['html_url']}")
        return 0
    head = gh_api("GET", f"/repos/{repo}/git/ref/heads/main")["object"]["sha"]
    version = gh_api("GET", f"/repos/{repo}/contents/services/{SERVICE}/VERSION?ref={head}")
    on_main = base64.b64decode(version["content"]).decode().strip()
    if on_main != BASELINE:
        fail(f"main has {SERVICE} {on_main}, not {BASELINE}: run scripts/reset.py github-actions first.")
    commit = gh_api("GET", f"/repos/{repo}/git/commits/{head}")
    tree = gh_api("GET", f"/repos/{repo}/git/trees/{commit['tree']['sha']}?recursive=1")
    main_files = [e["path"] for e in tree["tree"] if e["type"] == "blob" and e["path"].startswith(f"services/{SERVICE}/")]
    new_tree = gh_api("POST", f"/repos/{repo}/git/trees",
                      {"base_tree": commit["tree"]["sha"], "tree": tree_entries(main_files, state_files())})
    new_commit = gh_api("POST", f"/repos/{repo}/git/commits", {"message": TITLE, "tree": new_tree["sha"],
                                                                "parents": [head], "author": AUTHOR})
    branch = free_branch(repo)
    gh_api("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]})
    pull = gh_api("POST", f"/repos/{repo}/pulls", {"title": TITLE, "head": branch, "base": "main", "body": BODY})
    print(f"Opened {pull['html_url']} from {branch}. Merge it with Squash and merge.")
    return 0


def close_pulls(repo):
    for pull in open_pulls(repo):
        gh_api("PATCH", f"/repos/{repo}/pulls/{pull['number']}", {"state": "closed"})
        print(f"Closed #{pull['number']} ({pull['head']['ref']})")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["open", "close", "status"])
    args = parser.parse_args()
    if not gh_signed_in():
        fail("gh isn't signed in: gh auth login. The pull request is yours on GitHub.")
    repo = repository()
    if args.action == "open":
        return open_pull(repo)
    if args.action == "close":
        return close_pulls(repo)
    pulls = open_pulls(repo)
    print("\n".join(f"#{p['number']} {p['html_url']} ({p['head']['ref']})" for p in pulls) or "No demo pull request is open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
