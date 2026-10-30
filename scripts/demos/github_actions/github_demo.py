"""What demo 14's scripts share: the demo's names, the GitHub App's calls, and
your own gh login's calls.

The scripts here run from the repository root like the shared ones, which
they import from scripts/. Two identities reach GitHub: the GitHub App (its
key in SSM), for what the pipeline and the bridge do, and your gh login, for
what needs the repository's admin or must show as yours: the environments,
the repository variable, the demo's pull request.
"""

import json
import subprocess
import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

from common import (ROOT, SSM_PREFIX, fail, get_parameter, github_api, github_app_jwt,  # noqa: E402,F401
                    github_app_token, put_parameter, ssm_client)

SERVICE, VERSION, BASELINE = "orders-service", "1.4.0", "1.3.0"
WORKFLOW = "release.yml"
TITLE = "ORD-12 Show the delivery estimate on the order page"
BRANCH = "demo-14/delivery-estimate"
ENVIRONMENTS = ("staging", "production")
CANCELLED = "Cancelled by reset.py before the next take."


class App:
    """The release-demo GitHub App on the repository, with an installation token."""

    def __init__(self, ssm=None):
        self.token, self.repository, self.installation = github_app_token(ssm or ssm_client())

    def call(self, method, path, body=None):
        return github_api(method, path, self.token, body, scheme="token")

    def status(self, method, path, body=None):
        """(HTTP status, reply), for calls whose refusal is an answer."""
        try:
            return 200, self.call(method, path, body)
        except urllib.error.HTTPError as error:
            return error.code, None


def gh(*args, input=None, check=True):
    """Your own gh login."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, input=input)
    except FileNotFoundError:
        fail("gh isn't installed here: brew install gh, then gh auth login.")
    if check and result.returncode != 0:
        fail(f"gh {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result


def gh_api(method, path, body=None, check=True):
    """A REST call as you. Returns the reply's JSON, or None for an empty one or a refusal with check=False."""
    args = ["api", "--method", method, path, "-H", "Accept: application/vnd.github+json"]
    result = gh(*args, *(["--input", "-"] if body is not None else []),
                input=json.dumps(body) if body is not None else None, check=check)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def gh_signed_in():
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def repository(ssm=None):
    return get_parameter(ssm or ssm_client(), "github/repository")


def release_runs(app, statuses=("waiting", "in_progress", "queued", "requested", "pending")):
    """release.yml's runs that haven't finished, newest first."""
    runs = []
    for status in statuses:
        reply = app.call("GET", f"/repos/{app.repository}/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=20")
        runs += (reply or {}).get("workflow_runs", [])
    return sorted({run["id"]: run for run in runs}.values(), key=lambda run: run["id"], reverse=True)


def demo_pulls(app, state="open"):
    """The demo's pull requests: its title, from a demo-14/ branch."""
    reply = app.call("GET", f"/repos/{app.repository}/pulls?state={state}&per_page=50") or []
    return [p for p in reply if p["title"] == TITLE and p["head"]["ref"].startswith("demo-14/")]
