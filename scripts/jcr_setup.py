"""Set up JFrog Container Registry for the release pipeline. Idempotent.

    python scripts/jcr_setup.py --local   JCR in Docker on this Mac
    python scripts/jcr_setup.py --host    on the EC2 tools host, through SSM Run Command

Run it after JCR's first start, once you have accepted its licence and set the
admin password in its UI. --local asks for that password; --host reads it
from SSM (jcr/admin-password).

JCR's free edition keeps the repository, user and permission REST APIs for
Artifactory Pro, and its Access API takes no password. So this script does
what the free edition allows and prints the rest as UI steps:

- it sets the Custom Base URL (https://artifactory-demo.es.nl on the host).
  Behind the Cloudflare tunnel JCR only sees plain HTTP, and without the base
  URL its links and redirects point at the wrong address;
- it checks the repositories <service>-candidates and <service>-releases for
  each service in services.yaml, plus docker-local and helm-local, and lists
  the UI steps for any that are missing;
- it creates the access token for the user release-pipeline through the
  token API, which does take the admin password, and stores it: in SSM and
  the host's tmpfs with --host, in the local secrets folder with --local.
  A stored token that still works is kept. Then Jenkins reloads.

The user release-pipeline and its permission (read, annotate and deploy on
those repositories, no delete) are UI steps, printed below when needed.
"""

import argparse
import base64
import getpass
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import ROOT, fail, get_parameter, put_parameter, services, ssm_client

JCR = "http://localhost:8082"
JENKINS = "http://localhost:8080"
LOCAL_SECRETS = Path(os.environ.get("RELEASE_DEMO_STATE") or Path(
    os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "release-demo") / "secrets"
HOST_SECRETS = Path("/run/release-demo/secrets")
UI = {"local": "http://localhost:8082", "host": "https://artifactory-demo.es.nl"}
USER = "release-pipeline"
TOKEN_SECONDS = 365 * 24 * 3600

USER_STEPS = [
    "Administration, User Management, Users, New user.",
    f"User Name: {USER}. Email: release-pipeline@example.invalid. Untick Can Update Profile.",
    "Tick Disable UI Access and Disable Internal Password Login (confirm the dialog). Leave the password empty, then Save.",
]
PERMISSION_STEPS = [
    "Administration, User Management, Permissions, New Permission. Name: release-pipeline.",
    "Resources: add the repositories {repos}.",
    f"Users: add {USER} with Read, Annotate and Deploy/Cache. Leave Delete/Overwrite and Manage off, then Create.",
]


class Form(dict):
    """A request body sent as application/x-www-form-urlencoded."""


class Client:
    def __init__(self, base, user, password):
        self.base = base
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, headers=None, auth=None):
        """Returns (status, parsed JSON or text). Never raises on HTTP errors.
        A str body goes as text/plain, a Form as a form, anything else as JSON."""
        data = None
        headers = {"Authorization": auth or self.auth, **(headers or {})}
        if isinstance(body, str):
            data = body.encode()
            headers["Content-Type"] = "text/plain"
        elif isinstance(body, Form):
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                status, raw = response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read().decode(errors="replace")
        except urllib.error.URLError as error:
            fail(f"Nothing answers at {self.base}: {error.reason}. Is the tools host up? scripts/tools.sh status")
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw


def wanted_repositories():
    keys = []
    for name in services():
        keys += [(f"{name}-candidates", "Generic"), (f"{name}-releases", "Generic")]
    return keys + [("docker-local", "Docker"), ("helm-local", "Helm")]


def missing_repositories(jcr):
    status, reply = jcr.call("GET", "/artifactory/api/repositories?type=local")
    if status != 200:
        fail(f"JCR didn't list its repositories ({status}): {str(reply)[:200]}")
    existing = {repo["key"] for repo in reply}
    return [(key, kind) for key, kind in wanted_repositories() if key not in existing]


def token_works(jcr, token):
    """Anonymous access is off, so listing repositories needs a valid token."""
    return jcr.call("GET", "/artifactory/api/repositories", auth=f"Bearer {token}")[0] == 200


def create_token(jcr):
    """Through the token API, which accepts the admin password in this edition.
    Returns the token, or None when the user doesn't exist yet."""
    status, reply = jcr.call("POST", "/artifactory/api/security/token", Form(
        username=USER, scope="member-of-groups:readers", expires_in=str(TOKEN_SECONDS)))
    if status == 200 and isinstance(reply, dict) and reply.get("access_token"):
        return reply["access_token"]
    if status in (400, 404) and "user" in str(reply).lower():
        return None
    fail(f"JCR didn't create the token ({status}): {str(reply)[:200]}")


def ui_steps(ui, groups):
    print(f"Still to do in the JCR UI at {ui}:")
    number = 0
    for steps in groups:
        for step in steps:
            number += 1
            print(f"  {number}. {step}")
    fail("Then run this again: it checks each one and carries on.")


def reload_jenkins(secrets_dir):
    password = (secrets_dir / "jenkins-admin-password").read_text().strip()
    jenkins = Client(JENKINS, "admin", password)
    status, crumb = jenkins.call("GET", "/crumbIssuer/api/json")
    if status != 200:
        print("Couldn't reach Jenkins. The token is saved; Jenkins picks it up at its next start.")
        return
    status, _ = jenkins.call("POST", "/configuration-as-code/reload",
                             headers={crumb["crumbRequestField"]: crumb["crumb"]})
    print("Jenkins reloaded its configuration: jcr-token is set" if status in (200, 302)
          else f"Jenkins didn't reload ({status}). Restart it to pick up the token.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--local", action="store_true", help="JCR in Docker on this Mac")
    where.add_argument("--host", action="store_true", help="on the EC2 tools host")
    args = parser.parse_args()

    ui = UI["host"] if args.host else UI["local"]
    ssm = ssm_client() if args.host else None
    password = (get_parameter(ssm, "jcr/admin-password") if args.host
                else os.environ.get("JCR_ADMIN_PASSWORD") or getpass.getpass("JCR admin password: "))
    jcr = Client(JCR, "admin", password)
    status, _ = jcr.call("GET", "/artifactory/api/system/version")
    if status == 401:
        fail("JCR refused the admin password. Use the one you set in JCR's UI.")
    if status != 200:
        fail(f"JCR answered {status}. Open {ui}, accept the licence and finish the setup wizard first.")

    status, reply = jcr.call("PUT", "/artifactory/api/system/configuration/baseUrl", ui)
    if status != 200:
        fail(f"JCR didn't take the base URL ({status}): {str(reply)[:200]}")
    print(f"  base URL: {ui}")

    missing = missing_repositories(jcr)
    if missing:
        ui_steps(ui, [[f"Administration, Repositories, Create a Repository, Local, {kind}. "
                       f"Repository Key: {key}, then Create Local Repository." for key, kind in missing]])
    print(f"  repositories: all {len(wanted_repositories())} there")

    current = None
    if args.host:
        current = ssm.get_parameter(Name="/release-demo/jcr/token", WithDecryption=True)["Parameter"]["Value"]
    elif (LOCAL_SECRETS / "jcr-token").exists():
        current = (LOCAL_SECRETS / "jcr-token").read_text().strip()
    token = None
    if current and current != "unset" and token_works(jcr, current):
        print(f"  token for {USER}: the stored one still works")
    else:
        token = create_token(jcr)
        if token is None:
            repos = ", ".join(key for key, _ in wanted_repositories())
            ui_steps(ui, [USER_STEPS, [step.format(repos=repos) for step in PERMISSION_STEPS]])
        print(f"  token for {USER}: created, valid for a year")

    if args.host:
        if token:
            put_parameter(ssm, "jcr/token", token)
            print("  saved the token to SSM")
        subprocess.run([sys.executable, str(ROOT / "tools-host" / "host_secrets.py"), str(HOST_SECRETS)], check=True)
        reload_jenkins(HOST_SECRETS)
    else:
        if token:
            LOCAL_SECRETS.mkdir(parents=True, exist_ok=True)
            target = LOCAL_SECRETS / "jcr-token"
            target.write_text(token + "\n")
            target.chmod(0o600)
            print(f"  saved the token to {target}")
        reload_jenkins(LOCAL_SECRETS)
    print(f"Done. The permission for {USER} is checked by the pipeline's first publish.")


if __name__ == "__main__":
    sys.exit(main())
