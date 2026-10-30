"""Switch demo 20's two checks on or off: the Migration check and the Rollback check.

    python scripts/demos/rollback-trap/checks.py on
    python scripts/demos/rollback-trap/checks.py off
    python scripts/demos/rollback-trap/checks.py status

on adds, and off removes, the two script lines in services.yaml under
orders-service's scripts, with one commit to main as the GitHub App,
"demo: rollback-trap checks on [skip ci]". The release pipeline reads them
from main, so the next release has them or not. Take A runs with them off,
the platform as it was before; takes B to D with them on. Versions without
migrations never see them either way.
"""

import argparse
import base64
import sys

import trap  # noqa: F401  (puts scripts/ on the path)
from common import fail, github_api, github_app_token, ssm_client

MARKER = "    # scripts/demos/rollback-trap/checks.py on|off adds or removes these two lines.\n"
LINES = ["    migration_check: scripts/demos/rollback-trap/migration_check.py\n",
         "    rollback_check: scripts/demos/rollback-trap/rollback_check.py\n"]


def switched(text, on):
    """services.yaml with the two lines after the marker, or without them."""
    if MARKER not in text:
        fail("services.yaml has no line for the checks to go under: "
             "'# scripts/demos/rollback-trap/checks.py on|off adds or removes these two lines.'")
    without = "".join(line for line in text.splitlines(keepends=True) if line not in LINES)
    return without.replace(MARKER, MARKER + "".join(LINES), 1) if on else without


def is_on(text):
    return all(line in text for line in LINES)


def on_main():
    token, repository, _ = github_app_token(ssm_client())
    path = f"/repos/{repository}/contents/services.yaml"
    current = github_api("GET", f"{path}?ref=main", token, scheme="token")
    return token, path, current["sha"], base64.b64decode(current["content"]).decode()


def switch(on, dry_run=False):
    """Commit the switch to main if it changes anything. Returns what it did, in a few words."""
    token, path, sha, text = on_main()
    word = "on" if on else "off"
    if is_on(text) == on:
        return f"the checks are {word} on main already"
    if dry_run:
        return f"would switch the checks {word} on main"
    body = {"message": f"demo: rollback-trap checks {word} [skip ci]", "sha": sha, "branch": "main",
            "content": base64.b64encode(switched(text, on).encode()).decode()}
    github_api("PUT", path, token, body, scheme="token")
    return f"switched the checks {word} on main"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("state", choices=["on", "off", "status"])
    args = parser.parse_args()
    if args.state == "status":
        print(f"The checks are {'on' if is_on(on_main()[3]) else 'off'} on main.")
        return 0
    print(switch(args.state == "on").capitalize() + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
