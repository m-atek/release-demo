"""Set up JFrog Container Registry for the release pipeline. Idempotent.

    python scripts/jcr_setup.py --local   JCR in Docker on this Mac
    python scripts/jcr_setup.py --host    on the EC2 tools host, through SSM Run Command

Run it once after the first start, when you have accepted JCR's licence in its
UI and set the admin password there. --local asks for that password; --host
reads it from SSM (jcr/admin-password). Then it sets, or creates:

- the Custom Base URL, https://artifactory-demo.es.nl on the host. Behind the
  Cloudflare tunnel JCR only sees plain HTTP, and without the base URL its UI
  stalls right after a successful login;
- per service in services.yaml, the Generic repositories <service>-candidates
  and <service>-releases, plus docker-local and helm-local;
- the user release-pipeline, with read and deploy rights on those repositories
  but no delete, so a stored package can't be overwritten;
- an access token for release-pipeline, which Jenkins uses as jcr-token.

Where JCR refuses an API call, the script prints the steps for the UI, waits
and checks the result; on the host, where nobody can press Enter, it prints
them and stops, so you do them and run it again. --local writes the token to
the local secrets folder; --host writes it to SSM and the host's tmpfs. Either
way Jenkins then reloads its configuration. A token that still works is kept.
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
import urllib.request
from pathlib import Path

from common import ROOT, fail, get_parameter, put_parameter, services, ssm_client

JCR = "http://localhost:8082"
JENKINS = "http://localhost:8080"
LOCAL_SECRETS = Path(os.environ.get("RELEASE_DEMO_STATE") or Path(
    os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "release-demo") / "secrets"
HOST_SECRETS = Path("/run/release-demo/secrets")
UI = {"local": "http://localhost:8082", "host": "https://artifactory-demo.es.nl"}
ui = UI["local"]
USER = "release-pipeline"
TOKEN_SECONDS = 365 * 24 * 3600


class Client:
    def __init__(self, base, user, password):
        self.base = base
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, headers=None):
        """Returns (status, parsed JSON or text). Never raises on HTTP errors.
        A str body goes as text/plain, anything else as JSON."""
        data = None
        headers = {"Authorization": self.auth, **(headers or {})}
        if isinstance(body, str):
            data = body.encode()
            headers["Content-Type"] = "text/plain"
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
            fail(f"Nothing answers at {self.base}: {error.reason}. Start the stack: scripts/tools.sh up --local")
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw


def by_hand(steps, check):
    """Print UI steps, wait for Enter, and confirm the result."""
    print("JCR didn't accept this through its API. In the JCR UI at " + ui + ":")
    for number, step in enumerate(steps, 1):
        print(f"  {number}. {step}")
    if not sys.stdin.isatty():
        fail("Do these steps in the UI, then run this again.")
    while True:
        input("Press Enter when it's done. ")
        if check():
            return
        print("Not there yet.")


def ensure_base_url(jcr, url):
    status, reply = jcr.call("PUT", "/artifactory/api/system/configuration/baseUrl", url)
    if status != 200:
        fail(f"JCR didn't take the base URL ({status}): {str(reply)[:200]}")
    print(f"  base URL: {url}")


def repositories():
    keys = []
    for name in services():
        keys += [(f"{name}-candidates", "generic"), (f"{name}-releases", "generic")]
    return keys + [("docker-local", "docker"), ("helm-local", "helm")]


def ensure_repository(jcr, key, package_type):
    exists = lambda: jcr.call("GET", f"/artifactory/api/repositories/{key}")[0] == 200
    if exists():
        print(f"  repository {key}: already there")
        return
    config = {"key": key, "rclass": "local", "packageType": package_type,
              "description": "release-demo, created by scripts/jcr_setup.py"}
    if package_type == "docker":
        config["dockerApiVersion"] = "V2"
    status, _ = jcr.call("PUT", f"/artifactory/api/repositories/{key}", config)
    if status in (200, 201) and exists():
        print(f"  repository {key}: created")
        return
    by_hand([f"Administration, Repositories, Create a Repository, Local, {package_type.capitalize()}.",
             f"Repository Key: {key}. Leave the rest as it is, then Create Local Repository."], exists)


def ensure_user(jcr):
    exists = lambda: jcr.call("GET", f"/access/api/v2/users/{USER}")[0] == 200
    if exists():
        print(f"  user {USER}: already there")
        return
    # Nobody signs in as this user. Its password is random and never stored.
    body = {"username": USER, "email": "release-pipeline@example.invalid", "password": base64.urlsafe_b64encode(os.urandom(18)).decode() + "A1!",
            "admin": False, "disable_ui_access": True, "internal_password_disabled": False}
    status, _ = jcr.call("POST", "/access/api/v2/users", body)
    if status in (200, 201) and exists():
        print(f"  user {USER}: created")
        return
    by_hand(["Administration, User Management, Users, New User.",
             f"Username: {USER}. Email: release-pipeline@example.invalid. Any strong password; nobody signs in with it.",
             "Tick 'Disable UI Access', then Save."], exists)


def ensure_permission(jcr, keys):
    path = f"/artifactory/api/v2/security/permissions/{USER}"
    granted = lambda: jcr.call("GET", path)[0] == 200
    body = {"name": USER, "repo": {"repositories": keys, "include-patterns": ["**"], "exclude-patterns": [],
                                   "actions": {"users": {USER: ["read", "write", "annotate"]}}}}
    status, _ = jcr.call("PUT", path, body)
    if status in (200, 201) and granted():
        print(f"  permission {USER}: read and deploy on {len(keys)} repositories, no delete")
        return
    by_hand(["Administration, User Management, Permissions, New Permission.",
             f"Name: {USER}. Resources: add the repositories {', '.join(keys)}.",
             f"Users: add {USER} with Read, Annotate and Deploy/Cache. Leave Delete/Overwrite off, then Create."], granted)


def create_token(jcr):
    body = {"username": USER, "scope": "applied-permissions/user", "expires_in": TOKEN_SECONDS,
            "description": "Jenkins release pipeline (jcr-token)"}
    status, reply = jcr.call("POST", "/access/api/v1/tokens", body)
    if status == 200 and isinstance(reply, dict) and reply.get("access_token"):
        print(f"  token for {USER}: created, valid for a year")
        return reply["access_token"]
    print(f"JCR didn't create the token through its API. In the JCR UI at {ui}:")
    print("  1. Administration, User Management, Access Tokens, Generate Token.")
    print(f"  2. Token scope: User. User name: {USER}. Expiration: 1 year. Generate, then copy it.")
    if not sys.stdin.isatty():
        fail("Then store it: python scripts/secrets.py set jcr/token --force")
    return getpass.getpass("Paste the token (it isn't shown): ").strip()


def token_works(token, key):
    """The token can read a pipeline repository, so it is release-pipeline's and still valid."""
    request = urllib.request.Request(f"{JCR}/artifactory/api/repositories/{key}",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except urllib.error.URLError:
        return False


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
    global ui
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--local", action="store_true", help="JCR in Docker on this Mac")
    where.add_argument("--host", action="store_true", help="on the EC2 tools host")
    args = parser.parse_args()

    if args.host:
        ui, ssm = UI["host"], ssm_client()
        password = get_parameter(ssm, "jcr/admin-password")
    else:
        password = os.environ.get("JCR_ADMIN_PASSWORD") or getpass.getpass("JCR admin password: ")
    jcr = Client(JCR, "admin", password)
    status, _ = jcr.call("GET", "/artifactory/api/system/version")
    if status == 401:
        fail("JCR refused that password. Use the admin password you set in JCR's UI.")
    if status != 200:
        fail(f"JCR answered {status}. Open {ui}, accept the licence and finish the setup wizard first.")

    ensure_base_url(jcr, ui)
    keys = repositories()
    for key, package_type in keys:
        ensure_repository(jcr, key, package_type)
    ensure_user(jcr)
    ensure_permission(jcr, [key for key, _ in keys])

    if args.host:
        current = ssm.get_parameter(Name="/release-demo/jcr/token", WithDecryption=True)["Parameter"]["Value"]
        if current != "unset" and token_works(current, keys[0][0]):
            print(f"  token for {USER}: the stored one still works")
        else:
            put_parameter(ssm, "jcr/token", create_token(jcr) or fail("No token: nothing saved."))
            print("Saved the token to SSM")
        subprocess.run([sys.executable, str(ROOT / "tools-host" / "host_secrets.py"), str(HOST_SECRETS)], check=True)
        reload_jenkins(HOST_SECRETS)
        return

    token = create_token(jcr)
    if not token:
        fail("No token: nothing saved.")
    LOCAL_SECRETS.mkdir(parents=True, exist_ok=True)
    target = LOCAL_SECRETS / "jcr-token"
    target.write_text(token + "\n")
    target.chmod(0o600)
    print(f"Saved the token to {target}")
    reload_jenkins(LOCAL_SECRETS)


if __name__ == "__main__":
    sys.exit(main())
