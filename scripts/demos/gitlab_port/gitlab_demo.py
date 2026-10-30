"""What demo 17's scripts share: GitLab.com's API as the demo account, the
project and its runner, the runner's unit on the tools host, and the switch.

The scripts here run from the repository root like the shared ones, which
they import from scripts/. They run on the Mac: the account's token,
gitlab/api-token, is for the Mac only, and the tools host may not read it.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

import host  # noqa: E402
from common import ROOT, SSM_PREFIX, fail, get_parameter, put_parameter, ssm_client  # noqa: E402,F401

API = "https://gitlab.com/api/v4"
PROJECT = "release-demo"
RUNNER = "tools-host"
TRIGGER = "release-demo Slack handler"
UNIT = "release-demo-gitlab-runner"
SWITCH = "ci/orders-service"
USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


class GitLab:
    """GitLab.com's REST API as the demo account, with gitlab/api-token."""

    def __init__(self, ssm=None):
        if host.on_host():
            fail("Demo 17's scripts run on the Mac: the tools host may not read gitlab/api-token.")
        self.ssm = ssm or ssm_client()
        self.token = get_parameter(self.ssm, "gitlab/api-token")

    def call(self, method, path, body=None, ok=(200, 201, 202, 204)):
        """The reply's JSON, or None for an empty one. A status outside ok raises GitLabError."""
        request = urllib.request.Request(API + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"PRIVATE-TOKEN": self.token, "Content-Type": "application/json",
                                                  "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        if status not in ok:
            raise GitLabError(status, raw.decode(errors="replace")[:300], f"{method} {path}")
        return json.loads(raw) if raw.strip() else None

    def username(self):
        return self.call("GET", "/user")["username"]

    def project(self, missing_ok=False):
        path = urllib.parse.quote(f"{self.username()}/{PROJECT}", safe="")
        try:
            return self.call("GET", f"/projects/{path}")
        except GitLabError as error:
            if error.status == 404 and missing_ok:
                return None
            raise

    def runners(self, project_id):
        """The project's own runners, with their details."""
        found = self.call("GET", f"/projects/{project_id}/runners?type=project_type&per_page=100") or []
        return [self.call("GET", f"/runners/{runner['id']}") for runner in found]


class GitLabError(Exception):
    def __init__(self, status, detail, what):
        super().__init__(f"GitLab {what}: {status} {detail}")
        self.status = status


def runner_unit(action):
    """systemctl start, stop or is-active for the runner on the tools host, through SSM Run Command."""
    code, out, err = host.Host().shell(f"systemctl {action} {UNIT}; systemctl is-active {UNIT} || true")
    return out.strip().splitlines()[-1] if out.strip() else (err.strip() or f"exit {code}")


def install_runner():
    """tools-host/gitlab-runner.sh from main on the host, as boot.sh runs it: installs and registers once."""
    code, out, err = host.Host().shell(f"cd {host.REPO_DIR} && git pull --ff-only -q || true; "
                                       f"{host.REPO_DIR}/tools-host/gitlab-runner.sh", timeout=300)
    if code != 0:
        fail(f"gitlab-runner.sh failed on the tools host: {(err or out).strip()[-300:]}")
    return out


def switch(ssm, value=None):
    """The CI /release orders-service starts: read it, or set it."""
    if value:
        put_parameter(ssm, SWITCH, value, secure=False)
        return value
    try:
        current = ssm.get_parameter(Name=SSM_PREFIX + SWITCH)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return "jenkins (not created yet)"
    return "jenkins" if current == "unset" else current
