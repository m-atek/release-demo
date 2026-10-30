"""Point the GitHub App's webhook at the approval bridge, with a secret.

    python scripts/demos/github_actions/app_hook.py            set the webhook's URL and secret
    python scripts/demos/github_actions/app_hook.py --check    what the app and its installation have, changing nothing

The app delivers the "Deployment protection rule" event to the bridge's
/github route, signed with a secret generated here into SSM the first time
(bridge/github-webhook-secret), which the bridge checks. As the app itself,
with its JWT: PATCH /app/hook/config. Run it once the bridge is applied
(infra/approval_bridge.tf writes bridge/url).

GitHub has no API for the rest, which you do once in the app's settings
(github.com, Settings, Developer settings, GitHub Apps, the app, Edit): under
General, Webhook, tick Active; under Permissions and events, Repository
permissions, Deployments: Read and write, Actions: Read-only, and under
Subscribe to events, Deployment protection rule; save, then accept the new
permissions on the installation (Install App, the gear, Review request).
--check says which of these are still missing. Values are never printed.
"""

import argparse
import os
import sys
import urllib.error

from github_demo import SSM_PREFIX, fail, github_api, github_app_jwt, put_parameter, ssm_client

WANTED_PERMISSIONS = {"deployments": "write", "actions": "read"}
WANTED_EVENT = "deployment_protection_rule"


def bridge_url(ssm):
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + "bridge/url")["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        value = "unset"
    if value == "unset":
        fail("The approval bridge isn't applied yet: no bridge/url in SSM. Apply infra/ (scripts/tf.sh apply) first.")
    return value.rstrip("/") + "/github"


def webhook_secret(ssm):
    """The secret GitHub signs with, generated once into SSM."""
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + "bridge/github-webhook-secret", WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        fail("bridge/github-webhook-secret doesn't exist yet. Apply infra/ (scripts/tf.sh apply) first.")
    if value == "unset":
        value = os.urandom(32).hex()  # not the secrets module: scripts/secrets.py shadows it
        put_parameter(ssm, "bridge/github-webhook-secret", value)
        print("bridge/github-webhook-secret: generated and stored in SSM")
    return value


def missing(app, installation):
    """What the app's settings and its installation still lack, as lines for you."""
    lines = []
    for permission, level in WANTED_PERMISSIONS.items():
        have = (app.get("permissions") or {}).get(permission)
        if have != level and not (level == "read" and have == "write"):
            lines.append(f"app: Repository permissions, {permission.capitalize()}: "
                         f"{'Read and write' if level == 'write' else 'Read-only'} (now {have or 'none'})")
        granted = (installation.get("permissions") or {}).get(permission)
        if granted != have and have:
            lines.append(f"installation: accept the new {permission} permission (Install App, the gear, Review request)")
    if WANTED_EVENT not in (app.get("events") or []):
        lines.append("app: Subscribe to events, Deployment protection rule")
    elif WANTED_EVENT not in (installation.get("events") or []):
        lines.append("installation: accept the Deployment protection rule event (Install App, the gear, Review request)")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="show the app's webhook and permissions, change nothing")
    args = parser.parse_args()

    ssm = ssm_client()
    jwt = github_app_jwt(ssm)
    repository = ssm.get_parameter(Name=SSM_PREFIX + "github/repository")["Parameter"]["Value"]
    if not args.check:
        url = bridge_url(ssm)
        github_api("PATCH", "/app/hook/config", jwt,
                   {"url": url, "content_type": "json", "secret": webhook_secret(ssm), "insecure_ssl": "0"})
        print(f"webhook: {url}, JSON, signed with bridge/github-webhook-secret")

    config = github_api("GET", "/app/hook/config", jwt)
    app = github_api("GET", "/app", jwt)
    try:
        installation = github_api("GET", f"/repos/{repository}/installation", jwt)
    except urllib.error.HTTPError as error:
        fail(f"The app isn't installed on {repository} ({error.code}).")
    print(f"app {app['slug']}: webhook {config.get('url')}, {config.get('content_type')}, "
          f"secret {'set' if config.get('secret') else 'not set'}")
    todo = missing(app, installation)
    todo.append("app: General, Webhook, Active ticked (GitHub has no API to show or set it)")
    print("\nIn the app's settings on GitHub, if not done yet:" if todo else "")
    for line in todo:
        print(f"  - {line}")
    return 1 if len(todo) > 1 else 0


if __name__ == "__main__":
    sys.exit(main())
