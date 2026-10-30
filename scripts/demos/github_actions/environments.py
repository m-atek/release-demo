"""The repository's environments for release.yml, with your own gh login: it
needs the repository's admin.

    python scripts/demos/github_actions/environments.py --apply                staging and production, as below
    python scripts/demos/github_actions/environments.py --check                the settings, for the checks and the video
    python scripts/demos/github_actions/environments.py --apply --bridge-test  also the day-one environment bridge-test

- staging and production deploy from main only, a custom branch policy;
- production has no required reviewers and no wait timer, the GitHub App as
  its only protection rule, and administrators may not bypass it;
- bridge-test, for the first check only, is production's twin;
- the repository variable AWS_ACCOUNT_ID, from your AWS sign-in, so release.yml
  names its roles without the account id in the repository. It is not a secret.

--check also counts the repository's and the environments' secrets, which
must be none. Safe to run twice: it changes only what differs.
"""

import argparse
import sys

from github_demo import ENVIRONMENTS, fail, gh_api, gh_signed_in, get_parameter, repository, ssm_client

PROTECTED = ("production", "bridge-test")


def wanted_environment():
    return {"wait_timer": 0, "prevent_self_review": False, "reviewers": [],
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            "can_admins_bypass": False}


NOT_OFFERED = ("GitHub doesn't offer the GitHub App as a protection rule for {names} yet: the app needs "
               "Deployments: Read and write and the event Deployment protection rule, both accepted on its "
               "installation. python scripts/demos/github_actions/app_hook.py --check lists what is missing; "
               "then run this again.")


def apply(repo, app_id, names, account_id):
    refused = []
    for name in names:
        gh_api("PUT", f"/repos/{repo}/environments/{name}", wanted_environment())
        print(f"{name}: no reviewers, no wait timer, deploys from custom branches, no admin bypass")
        policies = gh_api("GET", f"/repos/{repo}/environments/{name}/deployment-branch-policies")["branch_policies"]
        if not any(p["name"] == "main" and p.get("type", "branch") == "branch" for p in policies):
            gh_api("POST", f"/repos/{repo}/environments/{name}/deployment-branch-policies", {"name": "main", "type": "branch"})
            print(f"{name}: deploys from main")
        for other in [p for p in policies if p["name"] != "main"]:
            print(f"{name}: also allows {other['name']}: remove it in the environment's settings")
        if name in PROTECTED:
            rules = gh_api("GET", f"/repos/{repo}/environments/{name}/deployment_protection_rules")
            if not any(r["app"]["id"] == int(app_id) for r in rules["custom_deployment_protection_rules"]):
                # GitHub answers 404 until the app's settings include Deployments and the event: the rest goes on.
                if gh_api("POST", f"/repos/{repo}/environments/{name}/deployment_protection_rules",
                          {"integration_id": int(app_id)}, check=False) is None:
                    refused.append(name)
                    print(f"{name}: GitHub refused the GitHub App as its protection rule")
                else:
                    print(f"{name}: the GitHub App is its protection rule")
    current = gh_api("GET", f"/repos/{repo}/actions/variables/AWS_ACCOUNT_ID", check=False)
    if current is None:
        gh_api("POST", f"/repos/{repo}/actions/variables", {"name": "AWS_ACCOUNT_ID", "value": account_id})
        print("variable AWS_ACCOUNT_ID: set")
    elif current["value"] != account_id:
        gh_api("PATCH", f"/repos/{repo}/actions/variables/AWS_ACCOUNT_ID", {"name": "AWS_ACCOUNT_ID", "value": account_id})
        print("variable AWS_ACCOUNT_ID: updated")
    if refused:
        fail(NOT_OFFERED.format(names=" and ".join(refused)))


def problems(name, environment, rules, policies, app_id, secrets):
    """What differs from the wanted settings, as lines."""
    found = []
    types = {rule["type"]: rule for rule in environment.get("protection_rules", [])}
    if "required_reviewers" in types:
        found.append("has required reviewers")
    if types.get("wait_timer", {}).get("wait_timer"):
        found.append("has a wait timer")
    if environment.get("can_admins_bypass", True):
        found.append("lets administrators bypass its rules: untick 'Allow administrators to bypass configured "
                     "protection rules' in the environment's settings")
    branch = environment.get("deployment_branch_policy") or {}
    if not branch.get("custom_branch_policies") or [p["name"] for p in policies] != ["main"]:
        found.append("deploys from more than main")
    apps = [rule["app"]["id"] for rule in rules]
    if name in PROTECTED and apps != [int(app_id)]:
        found.append(f"has protection rules {apps or 'none'}, not the GitHub App alone")
    if name not in PROTECTED and apps:
        found.append("has a protection rule")
    if secrets:
        found.append(f"holds {secrets} secret(s)")
    return found


def check(repo, app_id, names):
    bad = 0
    count = gh_api("GET", f"/repos/{repo}/actions/secrets")["total_count"]
    print(f"repository: {count} secret(s)" + ("" if count == 0 else ": there must be none"))
    bad += count != 0
    variables = [v["name"] for v in gh_api("GET", f"/repos/{repo}/actions/variables")["variables"]]
    print(f"repository: variables {', '.join(variables) or 'none'}")
    if "AWS_ACCOUNT_ID" not in variables:
        print("  FAIL repository has no variable AWS_ACCOUNT_ID: release.yml's jobs name their AWS roles with it. Run --apply.")
        bad += 1
    for name in names:
        environment = gh_api("GET", f"/repos/{repo}/environments/{name}", check=False)
        if environment is None:
            print(f"{name}: missing. Run --apply.")
            bad += 1
            continue
        rules = gh_api("GET", f"/repos/{repo}/environments/{name}/deployment_protection_rules")["custom_deployment_protection_rules"]
        policies = gh_api("GET", f"/repos/{repo}/environments/{name}/deployment-branch-policies")["branch_policies"]
        secrets = gh_api("GET", f"/repos/{repo}/environments/{name}/secrets")["total_count"]
        types = [rule["type"] for rule in environment.get("protection_rules", [])]
        print(f"{name}: reviewers {'yes' if 'required_reviewers' in types else 'none'}, "
              f"admin bypass {'on' if environment.get('can_admins_bypass', True) else 'off'}, "
              f"branches {', '.join(p['name'] for p in policies) or 'any'}, "
              f"rules {', '.join(rule['app']['slug'] for rule in rules) or 'none'}, secrets {secrets}")
        for line in problems(name, environment, rules, policies, app_id, secrets):
            print(f"  FAIL {name} {line}")
            bad += 1
    print("As release.yml needs it." if not bad else f"{bad} thing(s) to fix.")
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--bridge-test", action="store_true", help="also the day-one environment bridge-test")
    args = parser.parse_args()

    if not gh_signed_in():
        fail("gh isn't signed in: gh auth login, as the repository's admin.")
    ssm = ssm_client()
    repo, app_id = repository(ssm), get_parameter(ssm, "github/app-id")
    names = [*ENVIRONMENTS, *(["bridge-test"] if args.bridge_test else [])]
    if args.apply:
        from common import aws_session

        apply(repo, app_id, names, aws_session().client("sts").get_caller_identity()["Account"])
    return check(repo, app_id, names)


if __name__ == "__main__":
    sys.exit(main())
