"""The ruleset on main that requires demo 2's checks before a merge.

    python scripts/demos/terraform/ruleset.py           print it, as GitHub's API takes it
    python scripts/demos/terraform/ruleset.py --apply   create or update it with your gh login

It requires terraform fmt, terraform validate, tflint, Checkov and Conftest,
each from the GitHub App (the check runs Jenkins publishes through it). Two
actors may go past it: the GitHub App, so stage.py and reset.py still push
to main, and repository admins, so you can still push merged work. A pull
request then shows "Merging is blocked" until its checks pass. Run it with
your own gh login: the GitHub App has no administration permission.
"""

import argparse
import json
import subprocess
import sys

import stack
import checks
from common import fail, get_parameter, ssm_client

NAME = "main: infrastructure checks"
ADMIN_ROLE = 5  # GitHub's id for the repository role Admin


def ruleset(app_id):
    return {
        "name": NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [
            {"actor_id": int(app_id), "actor_type": "Integration", "bypass_mode": "always"},
            {"actor_id": ADMIN_ROLE, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        ],
        "rules": [{
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": name, "integration_id": int(app_id)} for name in checks.CHECKS],
            },
        }],
    }


def gh(*args, body=None):
    result = subprocess.run(["gh", "api", *args] + (["--input", "-"] if body else []), capture_output=True, text=True,
                            input=json.dumps(body) if body else None)
    if result.returncode != 0:
        fail(f"gh api {' '.join(args)}: {result.stderr.strip()}")
    return json.loads(result.stdout or "null")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="create or update it with your gh login")
    args = parser.parse_args()
    ssm = ssm_client()
    body = ruleset(get_parameter(ssm, "github/app-id"))
    if not args.apply:
        print(json.dumps(body, indent=2))
        return
    repository = get_parameter(ssm, "github/repository")
    existing = [r for r in gh(f"repos/{repository}/rulesets") if r["name"] == NAME]
    if existing:
        gh("-X", "PUT", f"repos/{repository}/rulesets/{existing[0]['id']}", body=body)
        print(f"Updated the ruleset \"{NAME}\" on {repository}")
    else:
        gh("-X", "POST", f"repos/{repository}/rulesets", body=body)
        print(f"Created the ruleset \"{NAME}\" on {repository}")
    print("Required: " + ", ".join(checks.CHECKS) + f". Bypass: the GitHub App and repository admins. Folder: {stack.relative(stack.STACK)}")


if __name__ == "__main__":
    sys.exit(main())
