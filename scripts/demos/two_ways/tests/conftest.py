"""What video 28's tests share: the run state in a temporary folder, and fakes for ServiceNow, JFrog and Lambda.
Nothing here leaves the machine, whoever is signed in.

    .venv/bin/python -m pytest -q -p no:cacheprovider scripts/demos/two_ways/tests
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

# No test can reach a real account, whatever it ends up calling: no profile, no credentials, no instance role.
for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(name, None)
os.environ.update(AWS_PROFILE="no-such-profile-in-tests", AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                  AWS_EC2_METADATA_DISABLED="true")

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
STATES = ROOT / "demo-states" / "orders-service"
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))
sys.path.insert(2, str(HERE.parents[1] / "tests"))  # pipeline_calls

import change  # noqa: E402
import cheat  # noqa: E402
import deploy  # noqa: E402
import notify  # noqa: E402
import publish  # noqa: E402
import run_state  # noqa: E402

SAM, ALEX = "sam-id", "alex-id"
PIPELINE_TOKEN, ADMIN_PASSWORD = "pipeline-token-not-a-real-one", "admin-password-not-a-real-one"
USERS = {SAM: {"first_name": "Sam", "name": "Sam Developer", "user_name": "sam.developer"},
         ALEX: {"first_name": "Alex", "name": "Alex Change Manager", "user_name": "alex.change.manager"},
         "pipeline-id": {"first_name": "Release", "name": "Release Pipeline", "user_name": "release.pipeline"},
         "notes-id": {"first_name": "AI", "name": "AI Notes", "user_name": "ai.notes"}}


@pytest.fixture(autouse=True)
def no_real_account(monkeypatch):
    """common.aws_session takes your release-demo profile when it finds one, so a fixture that misses one
    read would read a real secret from SSM. Here that is an error at once."""
    import boto3

    def refused(*args, **kwargs):
        raise AssertionError("A test reached a real AWS session: fake that call.")

    monkeypatch.setattr(boto3, "Session", refused)
    monkeypatch.setattr(boto3, "client", refused)


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


def by_path(name, path):
    """A module by its path: scripts/reset.py and this folder's reset.py share a name."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def conditions(query):
    return dict(part.split("=", 1) for part in query.split("^") if "=" in part)


class Now:
    """ServiceNow as the Table API and the Change Management API answer it, for one normal change waiting at Assess.
    Set forced_state to answer as a business rule would that keeps a new approval at requested."""

    url = "https://servicenow.example"

    def __init__(self, approval="requested", state="-4", requested_by=SAM, forced_state=None, cancel_from_implement=True):
        self.change = {"sys_id": "change-id", "number": "CHG0030120", "state": state, "approval": approval,
                       "requested_by": {"link": "https://servicenow.example/x", "value": requested_by} if requested_by else ""}
        self.rows = [{"sys_id": "alex-row", "sysapproval": "change-id", "approver": ALEX, "state": "requested", "sys_created_by": "system"}]
        self.work_notes, self.close_notes, self.forced_state = [], [], forced_state
        self.cancel_from_implement = cancel_from_implement

    def _rows(self, query):
        wanted = conditions(query)
        return [row for row in self.rows if all(row.get(key) == value for key, value in wanted.items())]

    def find(self, table, query, fields=None):
        if table == "sys_user" and query.startswith("user_name="):
            name = query.removeprefix("user_name=")
            return next(({"sys_id": key} for key, user in USERS.items() if user["user_name"] == name), None)
        if table == "sys_user":
            return USERS.get(query.removeprefix("sys_id="))
        if table == "change_request":
            return dict(self.change) if conditions(query).get("number") == self.change["number"] else None
        if table == "sysapproval_approver":
            found = self._rows(query)
            return dict(found[0]) if found else None
        raise AssertionError(f"Nothing here reads {table}")

    def call(self, method, path, body=None, **query):
        if method == "GET" and path.startswith("/api/now/table/change_request/"):
            return {"approval": self.change["approval"], "state": self.change["state"]}
        if method == "GET" and path == "/api/now/table/sysapproval_approver":
            return [{"sys_id": row["sys_id"], "approver": {"value": row["approver"]}} for row in self._rows(query["sysparm_query"])]
        if method == "POST" and path == "/api/now/table/sysapproval_approver":
            row = {"sys_id": f"row-{len(self.rows)}", "sys_created_by": "release-demo.setup", **body}
            row["state"] = self.forced_state or row["state"]
            self.rows.append(row)
            return dict(row)
        if method == "PATCH" and path.startswith("/api/now/table/sysapproval_approver/"):
            row = next(row for row in self.rows if row["sys_id"] == path.rsplit("/", 1)[1])
            if self.forced_state != "never approved":
                row.update(body)
            return dict(row)
        if method == "PATCH" and path == f"/api/now/table/change_request/{self.change['sys_id']}":
            self.work_notes.append(body["work_notes"])
            return {}
        if method == "PATCH" and path.startswith("/api/sn_chg_rest/change/"):
            if self.change["state"] == "-1" and not self.cancel_from_implement:
                raise SystemExit(f"ServiceNow PATCH {path}: 403 the state model refuses this")
            self.change["state"] = body["state"]
            if body.get("close_notes"):
                self.close_notes.append(body["close_notes"])
            return {}
        raise AssertionError(f"Nothing here calls {method} {path}")


class Store:
    """JFrog on localhost. As JCR does, it refuses an overwrite from a token without Delete/Overwrite, and takes the admin's."""

    def __init__(self, files=None, pipeline_may_overwrite=False, admin_refused=False):
        self.files, self.puts = dict(files or {}), []
        self.deployed_by = dict.fromkeys(self.files, "release-pipeline")
        self.pipeline_may_overwrite, self.admin_refused = pipeline_may_overwrite, admin_refused

    def _answer(self, method, path, who, data):
        if method == "GET" and path.startswith("api/storage/"):
            target = path.removeprefix("api/storage/")
            if target not in self.files:
                return 404, b"{}"
            return 200, json.dumps({"checksums": {"sha256": hashlib.sha256(self.files[target]).hexdigest()},
                                    "modifiedBy": self.deployed_by[target]}).encode()
        if method == "GET":
            return (200, self.files[path]) if path in self.files else (404, b"")
        self.puts.append((who, path))
        if who == "admin" and self.admin_refused:
            return 500, b"the admin's write failed"
        if who == "release-pipeline" and path in self.files and not self.pipeline_may_overwrite:
            return 403, b"not enough permissions to delete/overwrite"
        self.files[path], self.deployed_by[path] = data, who
        return 201, b""

    def as_pipeline(self, method, path, token, data=None, headers=None):
        assert token == PIPELINE_TOKEN
        return self._answer(method, path, "release-pipeline", data)

    def as_admin(self, method, path, password, data=None, headers=None):
        assert password == ADMIN_PASSWORD
        return self._answer(method, path, "admin", data)


class Waiter:
    def wait(self, FunctionName):
        pass


class Lambda:
    """orders-service's two functions, as deploy.py's role sees them."""

    def __init__(self, alias="37", app="1.26.0"):
        self.alias, self.app, self.calls, self.zips = alias, app, [], []

    def get_alias(self, FunctionName, Name):
        self.calls.append(("get_alias", FunctionName))
        return {"FunctionVersion": self.alias}

    def get_function_configuration(self, FunctionName, Qualifier):
        self.calls.append(("get_function_configuration", FunctionName))
        return {"Description": self.app}

    def update_function_code(self, FunctionName, ZipFile):
        self.calls.append(("update_function_code", FunctionName))
        self.zips.append(ZipFile)

    def get_waiter(self, name):
        return Waiter()

    def publish_version(self, FunctionName, Description):
        self.calls.append(("publish_version", FunctionName))
        return {"Version": "44"}

    def update_alias(self, FunctionName, Name, FunctionVersion):
        self.calls.append(("update_alias", FunctionName))

    def get_function_url_config(self, FunctionName, Qualifier):
        return {"FunctionUrl": f"https://{FunctionName}.example/"}

    def writes(self):
        return [call for call in self.calls if call[0] in ("update_function_code", "publish_version", "update_alias")]


def a_package(version="1.30.0"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("VERSION", f"{version}\n")
        archive.writestr("handler.py", "def handler(event, context):\n    return {}\n")
        archive.writestr("build_info.json", '{"commit": "abc1234"}\n')
    return out.getvalue()


def waiting_state(package=None, **extra):
    """A run stopped at Approval, as .release/state.json has it there."""
    stages = {name: {"status": "ok", "seconds": 5.0} for name in run_state.STAGES}
    stages.update({name: {"status": "waiting"} for name in ("Production", "Check production", "Record")})
    stages["Approval"] = {"status": "running"}
    state = {"service": "orders-service", "version": "1.30.0", "jira_key": "ORD-200", "jira_keys": ["ORD-200"], "option": "",
             "requested_by": "U0SAM000001", "slack": {"channel": "C0RELEASES", "ts": "1793.0001"},
             "build": {"number": "123", "url": "https://jenkins.example/job/release/123/"}, "started_at": 1793000000.0, "stages": stages,
             "change": {"sys_id": "change-id", "number": "CHG0030120", "type": "normal",
                        "url": "https://servicenow.example/change", "requested_by": SAM},
             "deploys": {"staging": {"number": "44", "previous_number": "43", "previous_version": "1.26.0",
                                     "url": "https://staging.example/", "deployed_at": 1793000100.0}}, **extra}
    if package is not None:
        state["package"] = {"path": PACKAGE_PATH, "sha256": hashlib.sha256(package).hexdigest(), "name": "orders-service-1.30.0.zip"}
    run_state.save(state)
    return state


PACKAGE_PATH = "orders-service-candidates/1.30.0/123/orders-service-1.30.0.zip"
HEX = re.compile(r"[0-9a-f]{64}")
NOTHING_REACHED = "Nothing reached production."


@pytest.fixture
def slack(monkeypatch):
    """Every message the run posts, as Slack would get it: nothing leaves the machine."""
    posted = []
    monkeypatch.setattr(notify, "post", lambda state: posted.append(notify.render(state)))
    monkeypatch.setattr(run_state, "now", lambda: 1793000215.0)
    return posted


@pytest.fixture
def no_waiting(monkeypatch):
    monkeypatch.setattr(change.time, "sleep", lambda seconds: pytest.fail("It looked again: the first look should have refused."))


@pytest.fixture
def stack(monkeypatch):
    """JFrog holding the build's package, and Lambda, reached through the real publish.download and deploy.main."""
    package = a_package()
    store = Store({PACKAGE_PATH: package})
    functions = Lambda()
    monkeypatch.setattr(publish, "request", store.as_pipeline)
    monkeypatch.setattr(deploy, "lambda_client", lambda name: functions)
    monkeypatch.setattr(deploy, "ssm_client", lambda: None)
    monkeypatch.setattr(deploy, "get_parameter", lambda ssm, name: PIPELINE_TOKEN)
    return package, store, functions


def stage_failed(name, seconds):
    """releaseStage's finish for a failed stage: the row, which posts the message."""
    state = run_state.load()
    state["stages"][name] = {"status": "failed", "seconds": seconds}
    run_state.save(state)
    notify.post(state)


def stopped(sn):
    """stopped() in releasePipeline.groovy, as test_refusals holds it: Slack's last line, then the cancel."""
    state = run_state.load()
    reason = (state.get("stop_reason") or "script returned exit code 1").rstrip(".")
    sys_argv, sys.argv = sys.argv, ["notify.py", "finish", "failed"]
    try:
        notify.main()
    finally:
        sys.argv = sys_argv
    state = run_state.load()
    assert not state.get("change", {}).get("closed") and not state.get("deploys", {}).get("production", {}).get("number")
    change.cancel(sn, state, f"The release stopped: {reason}. Nothing changed in production.")


def run_deploy(monkeypatch, environment):
    monkeypatch.setattr(sys, "argv", ["deploy.py", "orders-service", environment])
    deploy.main()


def swapped(store, monkeypatch):
    monkeypatch.setattr(cheat, "get_parameter", lambda ssm, name: PIPELINE_TOKEN if name == "jcr/token" else ADMIN_PASSWORD)
    assert cheat.swap("123", "1.30.0", ssm=object(), request=store.as_admin) == 0
