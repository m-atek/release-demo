"""The three rulesets that keep the coding agent's work to its own branches and pull requests.

    python scripts/demos/agent_ship/ruleset.py            print them, as GitHub's API takes them
    python scripts/demos/agent_ship/ruleset.py --apply    create or update them, active (your gh login)
    python scripts/demos/agent_ship/ruleset.py --on | --off   active or disabled, kept either way
    python scripts/demos/agent_ship/ruleset.py --check    print them as GitHub returns them, and check them

- "main: pull request and checks": main takes only pull requests, with one
  approving review, stale reviews dismissed on a push, the latest push
  approved by someone other than its pusher, and the checks Build, Test,
  Quality gate and SBOM and scan, each from the pipeline's GitHub App. No
  force pushes, no deletion.
- "agent branches only": on every other branch, creating, updating and
  deleting are restricted, except under agent/.
- "tags: pipeline only": tags are the pipeline's to make, move and remove.

The first two let the pipeline's app and repository admins past, exactly as
demo 2's ruleset does, so stage.py, reset.py and your own pushes still reach
main; the tags one lets only the pipeline's app past (tag.py and reset.py).
The coding agent's app is in no bypass list. Run it with your own gh login:
neither app may administer the repository. scripts/demo.sh up agent-ship
turns them on and down turns them off, so video 14's merge box shows no
bypass and demo 2's pull requests need no review.
"""

import argparse
import json
import subprocess
import sys

import agent  # noqa: F401  (puts scripts/ on the path)
from common import fail, get_parameter, service, ssm_client

MAIN, BRANCHES, TAGS = "main: pull request and checks", "agent branches only", "tags: pipeline only"
ADMIN_ROLE = 5  # GitHub's id for the repository role Admin


def rulesets(app_id, checks):
    pipeline = {"actor_id": int(app_id), "actor_type": "Integration", "bypass_mode": "always"}
    admins = {"actor_id": ADMIN_ROLE, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    return {
        MAIN: {"name": MAIN, "target": "branch", "enforcement": "active",
               "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
               "bypass_actors": [pipeline, admins],
               "rules": [
                   {"type": "pull_request", "parameters": {
                       "required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
                       "require_last_push_approval": True, "require_code_owner_review": False,
                       "required_review_thread_resolution": False}},
                   {"type": "required_status_checks", "parameters": {
                       "strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
                       "required_status_checks": [{"context": name, "integration_id": int(app_id)} for name in checks]}},
                   {"type": "non_fast_forward"},
                   {"type": "deletion"},
               ]},
        BRANCHES: {"name": BRANCHES, "target": "branch", "enforcement": "active",
                   "conditions": {"ref_name": {"include": ["~ALL"], "exclude": ["~DEFAULT_BRANCH", "refs/heads/agent/**"]}},
                   "bypass_actors": [pipeline, admins],
                   "rules": [{"type": "creation"}, {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}},
                             {"type": "deletion"}]},
        TAGS: {"name": TAGS, "target": "tag", "enforcement": "active",
               "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
               "bypass_actors": [pipeline],
               "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}]},
    }


def gh(*args, body=None):
    result = subprocess.run(["gh", "api", *args] + (["--input", "-"] if body is not None else []), capture_output=True,
                            text=True, input=json.dumps(body) if body is not None else None)
    if result.returncode != 0:
        fail(f"gh api {' '.join(args)}: {result.stderr.strip()}")
    return json.loads(result.stdout or "null")


def existing(repository):
    return {r["name"]: r for r in gh(f"repos/{repository}/rulesets") or [] if r["name"] in (MAIN, BRANCHES, TAGS)}


def apply(repository, wanted):
    found = existing(repository)
    for name, body in wanted.items():
        if name in found:
            gh("-X", "PUT", f"repos/{repository}/rulesets/{found[name]['id']}", body=body)
            print(f"Updated \"{name}\", active")
        else:
            gh("-X", "POST", f"repos/{repository}/rulesets", body=body)
            print(f"Created \"{name}\", active")


def switch(repository, enforcement):
    found = existing(repository)
    if not found:
        fail(f"No rulesets of video 16 on {repository} yet: run ruleset.py --apply first.")
    for name, ruleset in found.items():
        if ruleset.get("enforcement") == enforcement:
            print(f"\"{name}\": {enforcement} already")
            continue
        gh("-X", "PUT", f"repos/{repository}/rulesets/{ruleset['id']}", body={"enforcement": enforcement})
        print(f"\"{name}\": {enforcement}")


def check(repository, app_id, agent_app_id, checks):
    """One ok or FAIL line for each of the three, as GitHub returns them."""
    found = existing(repository)
    results = []
    for name in (MAIN, BRANCHES, TAGS):
        if name not in found:
            print(f"FAIL \"{name}\" isn't on {repository}")
            results.append(False)
            continue
        ruleset = gh(f"repos/{repository}/rulesets/{found[name]['id']}")
        bypass = [(a["actor_type"], a.get("actor_id")) for a in ruleset.get("bypass_actors") or []]
        agent_free = ("Integration", int(agent_app_id)) not in bypass if agent_app_id else True
        rules = sorted(r["type"] for r in ruleset.get("rules", []))
        ok = agent_free and ruleset.get("enforcement") == "active"
        if name == MAIN:
            status = next((r["parameters"] for r in ruleset["rules"] if r["type"] == "required_status_checks"), {})
            pinned = {(c["context"], c.get("integration_id")) for c in status.get("required_status_checks", [])}
            ok = ok and pinned == {(c, int(app_id)) for c in checks}
        print(f"{'ok  ' if ok else 'FAIL'} \"{name}\": {ruleset.get('enforcement')}, rules {', '.join(rules)}, "
              f"bypass {', '.join(f'{t} {i}' for t, i in bypass) or 'nobody'}"
              f"{'' if agent_free else ', and the coding agent app is in its bypass list'}")
        results.append(ok)
    return all(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--on", action="store_true")
    mode.add_argument("--off", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ssm = ssm_client()
    app_id = get_parameter(ssm, "github/app-id")
    checks = service("orders-service")["pull_requests"]["checks"]
    wanted = rulesets(app_id, checks)
    repository = get_parameter(ssm, "github/repository")
    if args.apply:
        apply(repository, wanted)
    elif args.on or args.off:
        switch(repository, "active" if args.on else "disabled")
    elif args.check:
        return 0 if check(repository, app_id, get_parameter(ssm, "github/agent-app-id"), checks) else 1
    else:
        print(json.dumps(list(wanted.values()), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
