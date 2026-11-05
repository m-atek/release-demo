"""What video 31's tests share: the run state in a temporary folder, no way to reach a real account,
and a fake ServiceNow whose answers follow a list. Nothing here leaves the machine.

    .venv/bin/python -m pytest -q -p no:cacheprovider scripts/demos/release_wait/tests
"""

import os
import sys
from pathlib import Path

import pytest

# No test can reach a real account, whatever it ends up calling: no profile, no credentials, no instance role.
for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(name, None)
os.environ.update(AWS_PROFILE="no-such-profile-in-tests", AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                  AWS_EC2_METADATA_DISABLED="true")

HERE = Path(__file__).resolve().parents[1]
SCRIPTS = HERE.parents[1]
ROOT = SCRIPTS.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(SCRIPTS))

import common  # noqa: E402
import run_state  # noqa: E402

WAITS = "1.33.0"          # the version services.yaml's approval_wait names
ANY_OTHER = "1.33.1"      # its neighbour, which waits as every other version does


def refuse(*args, **kwargs):
    raise AssertionError("a test tried to reach SSM")


@pytest.fixture(autouse=True)
def no_real_account_and_a_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ssm_client", refuse)
    monkeypatch.setattr(common, "aws_session", refuse)
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


class Clock:
    """time.time and time.sleep for one wait: only sleeping moves it."""

    def __init__(self, start=1793000000.0):
        self.now, self.slept = start, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeServiceNow:
    """answers: one per look at the change, each a dict for change_request, or an exception to raise, or a function
    of the clock's minute. The last repeats. approvers: who has approved, once the change reads approved."""

    url = "https://servicenow.example"

    def __init__(self, answers, approvers=("alex-id",)):
        self.answers, self.approvers, self.looks, self.calls = list(answers), list(approvers), 0, []

    def find(self, table, query, fields=None):
        users = {"alex-id": "Alex", "sam-id": "Sam", "pipeline-id": "Release"}
        if table == "sys_user" and query.startswith("sys_id="):
            return {"first_name": users.get(query.removeprefix("sys_id="))}
        if table == "sys_user" and query == "user_name=release.pipeline":
            return {"sys_id": "pipeline-id"}
        if table == "sysapproval_approver" or table == "sys_journal_field":
            return None
        return None

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/api/now/table/change_request/"):
            answer = self.answers[min(self.looks, len(self.answers) - 1)]
            self.looks += 1
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if method == "GET" and path == "/api/now/table/sysapproval_approver":
            if query.get("sysparm_query", "").endswith("state=rejected"):
                return []
            return [{"approver": {"value": who}} for who in self.approvers]
        return {}


def waiting_state(version=WAITS, **extra):
    state = {"service": "orders-service", "version": version, "jira_key": "ORD-140", "option": "", "build": {},
             "slack": {"channel": "C0RELEASES", "ts": "1793.0001"}, "started_at": 1793000000.0,
             "stages": {"Approval": {"status": "running"}},
             "change": {"sys_id": "change-id", "number": "CHG0030120", "type": "normal", "requested_by": "sam-id"},
             **extra}
    run_state.save(state)
    return state
