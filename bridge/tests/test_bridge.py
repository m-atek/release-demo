"""The approval bridge, without AWS, GitHub or ServiceNow: every call is a fake.

    .venv/bin/python -m pytest -q bridge/tests
"""

import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("approval_bridge", ROOT / "bridge" / "handler.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

SECRET = "test-webhook-secret, not a real one"
TOKEN = "test-servicenow-token, not a real one"
REPOSITORY = "m-atek/release-demo"
RUN = "18300000001"
CONTEXT = SimpleNamespace(invoked_function_arn="arn:aws:lambda:eu-west-1:123456789012:function:release-demo-approval-bridge")
SAM, ALEX = "a" * 32, "b" * 32
CHANGE_ID = "c" * 32
SSM_VALUES = {
    "bridge/github-webhook-secret": SECRET,
    "bridge/servicenow-token": TOKEN,
    "github/repository": REPOSITORY,
    "github/app-id": "123456",
    "github/app-private-key": "unused: app_jwt is faked",
    "servicenow/instance-url": "https://servicenow.example",
    "servicenow/release-pipeline-password": "test-password",
}


class FakeSSM:
    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption):
        assert WithDecryption and len(Names) <= 10
        return {"Parameters": [{"Name": n, "Value": self.values[n.removeprefix(bridge.SSM_PREFIX)]}
                               for n in Names if n.removeprefix(bridge.SSM_PREFIX) in self.values]}


class FakeLambda:
    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)


@pytest.fixture
def aws(monkeypatch):
    fakes = SimpleNamespace(ssm=FakeSSM(SSM_VALUES), lambda_=FakeLambda())
    monkeypatch.setattr(bridge, "aws", lambda name: {"ssm": fakes.ssm, "lambda": fakes.lambda_}[name])
    monkeypatch.setattr(bridge, "_parameters", {})
    return fakes


def handed_over(fakes):
    assert len(fakes.lambda_.invocations) == 1
    call = fakes.lambda_.invocations[0]
    assert call["FunctionName"] == CONTEXT.invoked_function_arn and call["InvocationType"] == "Event"
    return json.loads(call["Payload"])["work"]


def webhook(payload, event="deployment_protection_rule", secret=SECRET):
    body = json.dumps(payload)
    signature = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return {"rawPath": "/github", "requestContext": {"http": {"method": "POST"}}, "body": body,
            "headers": {"X-GitHub-Event": event, "X-Hub-Signature-256": signature, "Content-Type": "application/json"}}


def requested(environment="production", run=RUN, repository=REPOSITORY, ref="main"):
    return {"action": "requested", "environment": environment, "event": "push", "ref": ref, "sha": "f" * 40,
            "deployment_callback_url": f"https://api.github.com/repos/{repository}/actions/runs/{run}/deployment_protection_rule",
            "repository": {"full_name": repository}, "installation": {"id": 42}}


# The GitHub route

def test_a_signed_request_for_production_is_handed_over(aws):
    response = bridge.handler(webhook(requested()), CONTEXT)
    assert response["statusCode"] == 202
    assert handed_over(aws) == {"route": "github", "run_id": RUN, "environment": "production", "ref": "main"}


@pytest.mark.parametrize("change", [
    lambda e: e.update(headers={**e["headers"], "X-Hub-Signature-256": "sha256=" + "0" * 64}),
    lambda e: e["headers"].pop("X-Hub-Signature-256"),
    lambda e: e.update(body=e["body"].replace("production", "staging")),
])
def test_a_request_github_didnt_sign_is_refused(aws, change):
    event = webhook(requested())
    change(event)
    assert bridge.handler(event, CONTEXT)["statusCode"] == 401
    assert aws.lambda_.invocations == []


def test_without_a_webhook_secret_nothing_is_accepted(aws):
    aws.ssm.values["bridge/github-webhook-secret"] = "unset"
    assert bridge.handler(webhook(requested()), CONTEXT)["statusCode"] == 500


def test_githubs_ping_gets_a_plain_ok(aws):
    assert bridge.handler(webhook({"zen": "Keep it logically awesome."}, event="ping"), CONTEXT)["statusCode"] == 200
    assert aws.lambda_.invocations == []


@pytest.mark.parametrize("payload, event", [
    (requested(environment="staging"), "deployment_protection_rule"),
    (requested(repository="someone/fork"), "deployment_protection_rule"),
    ({**requested(), "action": "completed"}, "deployment_protection_rule"),
    ({**requested(), "deployment_callback_url": "https://example.com/approve"}, "deployment_protection_rule"),
    (requested(), "push"),
])
def test_anything_but_a_wait_on_this_repositorys_environments_is_ignored(aws, payload, event):
    assert bridge.handler(webhook(payload, event=event), CONTEXT)["statusCode"] == 202
    assert aws.lambda_.invocations == []


def test_only_posts_are_answered(aws):
    event = webhook(requested())
    event["requestContext"]["http"]["method"] = "GET"
    assert bridge.handler(event, CONTEXT)["statusCode"] == 405
    assert bridge.handler({**webhook(requested()), "rawPath": "/other"}, CONTEXT)["statusCode"] == 404


# The ServiceNow route

def servicenow_call(body, token=TOKEN):
    return {"rawPath": "/servicenow", "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body),
            "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}}


def test_servicenows_call_with_its_token_is_handed_over_by_sys_id_only(aws):
    response = bridge.handler(servicenow_call({"sys_id": CHANGE_ID, "approval": "approved", "run": "anything"}), CONTEXT)
    assert response["statusCode"] == 202
    assert handed_over(aws) == {"route": "servicenow", "sys_id": CHANGE_ID}


def test_a_call_without_servicenows_token_is_refused(aws):
    assert bridge.handler(servicenow_call({"sys_id": CHANGE_ID}, token="guess"), CONTEXT)["statusCode"] == 401
    event = servicenow_call({"sys_id": CHANGE_ID})
    del event["headers"]["Authorization"]
    assert bridge.handler(event, CONTEXT)["statusCode"] == 401
    assert aws.lambda_.invocations == []


@pytest.mark.parametrize("body", [{}, {"sys_id": "CHG0030050"}, {"sys_id": "c" * 31}])
def test_a_call_that_names_no_change_is_refused(aws, body):
    assert bridge.handler(servicenow_call(body), CONTEXT)["statusCode"] == 400


# The decision, as change.py wait makes it

def change(approval="requested", state="-4", type_="normal", number="CHG0030050"):
    return {"sys_id": CHANGE_ID, "number": number, "approval": approval, "state": state, "type": type_,
            "requested_by": SAM, "correlation_id": f"github-actions:{REPOSITORY}:{RUN}"}


NAMES = {ALEX: "Alex", SAM: "Sam"}.get


@pytest.mark.parametrize("record, approvals, verdict, text", [
    (None, [], "rejected", "No ServiceNow change for this run."),
    (change(), [], "waiting", "ServiceNow change CHG0030050 is waiting for approval."),
    (change("approved", "-2"), [{"state": "approved", "approver": ALEX}], "approved",
     "Approved by Alex in ServiceNow. Change CHG0030050."),
    (change("approved", "-2"), [{"state": "approved", "approver": SAM}], "rejected",
     "Approved by the person who asked for the release. Refused: the person who asks can't approve. Change CHG0030050."),
    (change("requested"), [{"state": "approved", "approver": SAM}], "rejected",
     "Approved by the person who asked for the release. Refused: the person who asks can't approve. Change CHG0030050."),
    (change("rejected"), [{"state": "rejected", "approver": ALEX}], "rejected",
     "Rejected by Alex in ServiceNow. Change CHG0030050."),
    (change("rejected"), [], "rejected", "Rejected in ServiceNow. Change CHG0030050."),
    (change("approved", type_="standard"), [], "approved",
     "Pre-approved in ServiceNow as a standard change. Change CHG0030050."),
    (change("approved"), [], "waiting", "ServiceNow change CHG0030050 is waiting for approval."),
    (change("approved", "3"), [{"state": "approved", "approver": ALEX}], "rejected",
     "ServiceNow change CHG0030050 is no longer open."),
    (change("requested", "4"), [], "rejected", "ServiceNow change CHG0030050 is no longer open."),
])
def test_the_bridge_decides_as_change_py_wait_does(record, approvals, verdict, text):
    assert bridge.decide(record, approvals, NAMES) == (verdict, text)


# The asynchronous part: ServiceNow read, the answer on the run

class FakeServiceNow:
    def __init__(self, record=None, approvals=(), asleep=False):
        self.record, self.list, self.asleep, self.queries = record, list(approvals), asleep, []

    def change(self, sys_id):
        self.queries.append(("sys_id", sys_id))
        return self.record if self.record and self.record["sys_id"] == sys_id else None

    def change_by_correlation(self, correlation):
        if self.asleep:
            raise bridge.Asleep()
        self.queries.append(("correlation", correlation))
        return self.record if self.record and self.record["correlation_id"] == correlation else None

    def approvals(self, record):
        return self.list

    first_name = staticmethod(NAMES)


class FakeGitHub:
    def __init__(self, pending=None, answers=None):
        self.calls, self.pending, self.answers = [], pending, dict(answers or {})

    def __call__(self, method, path, authorization, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/pending_deployments"):
            return (200, self.pending) if self.pending is not None else (404, None)
        return self.answers.get(body.get("environment_name") if body else None, (204, None))

    def reviews(self):
        return [(path.split("/")[-2], body) for method, path, body in self.calls if path.endswith("/deployment_protection_rule")]


@pytest.fixture
def world(aws, monkeypatch):
    world = SimpleNamespace(sn=FakeServiceNow(), github=FakeGitHub())
    monkeypatch.setattr(bridge.ServiceNow, "connect", classmethod(lambda cls: world.sn))
    monkeypatch.setattr(bridge, "call_github", world.github)
    monkeypatch.setattr(bridge, "installation_token", lambda: "test-installation-token")
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: None)
    return world


def test_a_waiting_change_gets_a_status_report_on_the_run(world):
    world.sn.record = change()
    bridge.handler({"work": {"route": "github", "run_id": RUN, "environment": "production", "ref": "main"}}, CONTEXT)
    assert world.sn.queries == [("correlation", f"github-actions:{REPOSITORY}:{RUN}")]
    assert world.github.reviews() == [(RUN, {"environment_name": "production",
                                             "comment": "ServiceNow change CHG0030050 is waiting for approval."})]


def test_an_approval_that_came_first_approves_the_run_at_once(world):
    world.sn.record, world.sn.list = change("approved", "-2"), [{"state": "approved", "approver": ALEX}]
    bridge.handler({"work": {"route": "github", "run_id": RUN, "environment": "production", "ref": "main"}}, CONTEXT)
    assert world.github.reviews() == [(RUN, {"environment_name": "production", "state": "approved",
                                             "comment": "Approved by Alex in ServiceNow. Change CHG0030050."})]


def test_a_run_with_no_change_is_rejected(world):
    bridge.handler({"work": {"route": "github", "run_id": RUN, "environment": "production", "ref": "main"}}, CONTEXT)
    assert world.github.reviews() == [(RUN, {"environment_name": "production", "state": "rejected",
                                             "comment": "No ServiceNow change for this run."})]


def test_a_run_from_another_branch_is_rejected_without_asking_servicenow(world):
    bridge.handler({"work": {"route": "github", "run_id": RUN, "environment": "production",
                             "ref": "refs/heads/demo-14/try"}}, CONTEXT)
    assert world.sn.queries == []
    assert world.github.reviews() == [(RUN, {"environment_name": "production", "state": "rejected",
                                             "comment": "Only main deploys to production. This run is from demo-14/try."})]


def test_a_sleeping_servicenow_is_named_on_the_run(world):
    world.sn.asleep = True
    bridge.handler({"work": {"route": "github", "run_id": RUN, "environment": "production", "ref": "main"}}, CONTEXT)
    (_, body), = world.github.reviews()
    assert "state" not in body and body["comment"].startswith("ServiceNow is asleep")


def test_alexs_approval_approves_the_environment_the_run_waits_on(world):
    world.sn.record, world.sn.list = change("approved", "-2"), [{"state": "approved", "approver": ALEX}]
    world.github.pending = [{"environment": {"name": "production"}}]
    bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT)
    assert world.github.reviews() == [(RUN, {"environment_name": "production", "state": "approved",
                                             "comment": "Approved by Alex in ServiceNow. Change CHG0030050."})]


def test_where_github_lists_no_pending_rule_every_environment_is_answered(world):
    world.sn.record, world.sn.list = change("rejected"), [{"state": "rejected", "approver": ALEX}]
    world.github.answers = {"bridge-test": (422, None)}
    bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT)
    assert [(env["environment_name"], env["state"]) for _, env in world.github.reviews()] == [
        ("production", "rejected"), ("bridge-test", "rejected")]


def test_an_approval_by_the_requester_is_refused_on_the_run(world):
    world.sn.record, world.sn.list = change("approved", "-2"), [{"state": "approved", "approver": SAM}]
    world.github.pending = [{"environment": {"name": "production"}}]
    bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT)
    (_, body), = world.github.reviews()
    assert body["state"] == "rejected" and body["comment"].startswith("Approved by the person who asked for the release.")


@pytest.mark.parametrize("record", [
    change(),  # still waiting: nothing to say
    {**change("approved"), "correlation_id": "azure-pipelines:org/release-demo:12"},
    {**change("approved"), "correlation_id": f"github-actions:someone/fork:{RUN}"},
    {**change("approved"), "correlation_id": ""},
])
def test_servicenow_calls_for_other_changes_tell_github_nothing(world, record):
    world.sn.record, world.sn.list = record, [{"state": "approved", "approver": ALEX}]
    bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT)
    assert world.github.calls == []


def test_the_work_never_raises(world, monkeypatch):
    monkeypatch.setattr(bridge.ServiceNow, "connect", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no"))))
    assert bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT) is None


def test_github_answering_5xx_is_asked_three_times(world):
    world.sn.record, world.sn.list = change("approved", "-2"), [{"state": "approved", "approver": ALEX}]
    world.github.answers = {"production": (502, None)}
    bridge.github_requested({"run_id": RUN, "environment": "production", "ref": "main"})
    assert len(world.github.reviews()) == 3


def test_a_comment_is_cut_to_githubs_1024_characters(world):
    bridge.review(REPOSITORY, RUN, "production", None, "x" * 2000)
    (_, body), = world.github.reviews()
    assert len(body["comment"]) == 1024


# The GitHub App's token

def test_the_apps_jwt_is_rs256_signed_for_nine_minutes():
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    now = int(time.time())
    token = bridge.app_jwt(123456, pem, now=now)
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": now - 60, "exp": now + 540, "iss": "123456"}


def test_the_installation_token_asks_for_deployments_and_actions_only(aws, monkeypatch):
    calls = []

    def fake(method, path, authorization, body=None):
        calls.append((method, path, authorization, body))
        if path.endswith("/installation"):
            return 200, {"id": 42}
        return 201, {"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(bridge, "call_github", fake)
    monkeypatch.setattr(bridge, "app_jwt", lambda app_id, key: "test-jwt")
    monkeypatch.setattr(bridge, "_token", {})
    assert bridge.installation_token() == "ghs_test"
    assert bridge.installation_token() == "ghs_test"  # kept, not asked again
    assert [(m, p, a) for m, p, a, _ in calls] == [("GET", f"/repos/{REPOSITORY}/installation", "Bearer test-jwt"),
                                                   ("POST", "/app/installations/42/access_tokens", "Bearer test-jwt")]
    assert calls[1][3] == {"repositories": ["release-demo"], "permissions": {"deployments": "write", "actions": "read"}}


# The Azure DevOps route (demo 22): production's Invoke REST API check, in callback mode

ORG = "demo-org"
AUTH = "test-job-access-token, not a real one"
IDS = {"projectid": "11111111-1111-1111-1111-111111111111", "planid": "22222222-2222-2222-2222-222222222222",
       "jobid": "33333333-3333-3333-3333-333333333333", "timelineid": "44444444-4444-4444-4444-444444444444",
       "taskinstanceid": "55555555-5555-5555-5555-555555555555"}
CHECK = {"planurl": f"https://dev.azure.com/{ORG}/", "hubname": "build", "buildid": "321", "authtoken": AUTH, **IDS}
AZURE_CORRELATION = f"azure-pipelines:{ORG}/release-demo:321"


def delivery(**overrides):
    headers = {name: value for name, value in {**CHECK, **overrides}.items()}
    return {"rawPath": "/azure-devops/check", "requestContext": {"http": {"method": "POST"}}, "body": "{}",
            "headers": {name.capitalize(): value for name, value in headers.items()}}


@pytest.fixture
def azure(aws):
    aws.ssm.values.update({"azure-devops/organization": ORG, "azure-devops/pipeline-id": "7"})
    return aws


def test_a_check_delivery_for_the_organization_is_answered_at_once_and_handed_over(azure):
    response = bridge.handler(delivery(), CONTEXT)
    assert response["statusCode"] == 200
    assert handed_over(azure) == {"route": "azure", "check": CHECK}


@pytest.mark.parametrize("overrides, status", [
    ({"planurl": "https://dev.azure.com/someone-else/"}, 403),
    ({"planurl": f"https://evil.example/{ORG}/"}, 403),
    ({"planid": "not-a-guid"}, 400),
    ({"buildid": "12a"}, 400),
    ({"authtoken": ""}, 400),
    ({"hubname": "build; rm"}, 400),
])
def test_a_delivery_from_elsewhere_or_without_the_runs_ids_gets_no_decision(azure, overrides, status):
    assert bridge.handler(delivery(**overrides), CONTEXT)["statusCode"] == status
    assert azure.lambda_.invocations == []


class FakeDevOps:
    """Azure DevOps' answers to a check's token, recorded."""

    def __init__(self, build=None, build_status=200):
        self.calls, self.build_status = [], build_status
        self.build_reply = build or {"definition": {"id": 7}, "sourceBranch": "refs/heads/main",
                                     "project": {"name": "release-demo"}}

    def __call__(self, devops, method, url, body=None, content_type="application/json"):
        self.calls.append((method, url.split("/_apis/")[1], body))
        assert devops.check["authtoken"] == AUTH
        if "/build/builds/" in url:
            return self.build_status, (self.build_reply if self.build_status == 200 else None)
        if url.endswith("/logs?api-version=7.1"):
            return 200, {"id": 9}
        return 200, None

    def lines(self):
        return [body.decode().strip() for method, url, body in self.calls if isinstance(body, bytes)]

    def events(self):
        return [body for method, url, body in self.calls if "/events?" in url]


class FakeDynamo:
    def __init__(self):
        self.items = {}

    def put_item(self, TableName, Item):
        assert TableName == bridge.CHECKS_TABLE
        self.items[Item["correlation_id"]["S"]] = Item

    def get_item(self, TableName, Key, ConsistentRead):
        item = self.items.get(Key["correlation_id"]["S"])
        return {"Item": item} if item else {}

    def delete_item(self, TableName, Key):
        self.items.pop(Key["correlation_id"]["S"], None)


@pytest.fixture
def devops(azure, world, monkeypatch):
    fake, table = FakeDevOps(), FakeDynamo()
    monkeypatch.setattr(bridge.AzureDevOps, "call", lambda self, *args, **kwargs: fake(self, *args, **kwargs))
    original = bridge.aws
    monkeypatch.setattr(bridge, "aws", lambda name: table if name == "dynamodb" else original(name))
    world.devops, world.table = fake, table
    return world


def azure_change(approval="requested", state="-4"):
    return {**change(approval, state), "correlation_id": AZURE_CORRELATION}


def test_a_waiting_change_is_kept_with_its_callback_and_named_in_the_checks_log(devops, capsys):
    devops.sn.record = azure_change()
    bridge.handler({"work": {"route": "azure", "check": CHECK}}, CONTEXT)
    assert devops.devops.lines() == ["ServiceNow change CHG0030050 is waiting for approval."]
    assert devops.devops.events() == []
    stored = devops.table.items[AZURE_CORRELATION]
    assert json.loads(stored["check"]["S"]) == CHECK and stored["log_id"] == {"N": "9"}
    assert int(stored["expires_at"]["N"]) - time.time() > 47 * 3600
    timeline = [body for method, url, body in devops.devops.calls if method == "PATCH"]
    assert timeline == [{"value": [{"id": IDS["taskinstanceid"], "log": {"id": 9}}], "count": 1}]
    assert AUTH not in capsys.readouterr().out


def test_an_approval_that_came_first_passes_the_check_at_once(devops):
    devops.sn.record, devops.sn.list = azure_change("approved", "-2"), [{"state": "approved", "approver": ALEX}]
    bridge.handler({"work": {"route": "azure", "check": CHECK}}, CONTEXT)
    assert devops.devops.lines() == ["Approved by Alex in ServiceNow. Change CHG0030050."]
    assert devops.devops.events() == [{"name": "TaskCompleted", "taskId": IDS["taskinstanceid"], "jobId": IDS["jobid"],
                                       "result": "succeeded"}]
    assert devops.table.items == {}


def test_a_forged_or_expired_token_gets_no_decision(devops):
    devops.devops.build_status = 401
    bridge.handler({"work": {"route": "azure", "check": CHECK}}, CONTEXT)
    assert devops.devops.events() == [] and devops.devops.lines() == [] and devops.table.items == {}


def test_a_run_from_another_branch_fails_the_check_with_the_branch_line(devops):
    devops.devops.build_reply = {"definition": {"id": 7}, "sourceBranch": "refs/heads/try", "project": {"name": "release-demo"}}
    bridge.handler({"work": {"route": "azure", "check": CHECK}}, CONTEXT)
    assert devops.devops.lines() == ["Only main deploys to production. This run is from try."]
    assert devops.devops.events()[0]["result"] == "failed"


def test_another_pipelines_run_fails_the_check(devops):
    devops.devops.build_reply = {"definition": {"id": 8}, "sourceBranch": "refs/heads/main", "project": {"name": "release-demo"}}
    bridge.handler({"work": {"route": "azure", "check": CHECK}}, CONTEXT)
    assert devops.devops.lines() == ["No ServiceNow change for this run."]
    assert devops.devops.events()[0]["result"] == "failed"


def test_alexs_approval_completes_the_waiting_check_with_its_stored_token(devops):
    devops.sn.record = azure_change()
    bridge.handler({"work": {"route": "azure", "check": CHECK}}, CONTEXT)
    devops.sn.record, devops.sn.list = azure_change("approved", "-2"), [{"state": "approved", "approver": ALEX}]
    bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT)
    assert devops.devops.lines() == ["ServiceNow change CHG0030050 is waiting for approval.",
                                     "Approved by Alex in ServiceNow. Change CHG0030050."]
    assert [e["result"] for e in devops.devops.events()] == ["succeeded"]
    assert devops.table.items == {}
    assert len([c for c in devops.devops.calls if c[1].endswith("/logs?api-version=7.1")]) == 1  # the same log


@pytest.mark.parametrize("approvals, approval, line", [
    ([{"state": "approved", "approver": SAM}], "approved",
     "Approved by the person who asked for the release. Refused: the person who asks can't approve. Change CHG0030050."),
    ([{"state": "rejected", "approver": ALEX}], "rejected", "Rejected by Alex in ServiceNow. Change CHG0030050."),
])
def test_a_rejection_or_the_requesters_approval_fails_the_waiting_check(devops, approvals, approval, line):
    devops.sn.record = azure_change()
    bridge.handler({"work": {"route": "azure", "check": CHECK}}, CONTEXT)
    devops.sn.record, devops.sn.list = azure_change(approval, "-2"), approvals
    bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT)
    assert devops.devops.lines()[-1] == line
    assert [e["result"] for e in devops.devops.events()] == ["failed"]


def test_a_decision_with_no_check_waiting_tells_azure_devops_nothing(devops):
    devops.sn.record, devops.sn.list = azure_change("approved", "-2"), [{"state": "approved", "approver": ALEX}]
    bridge.handler({"work": {"route": "servicenow", "sys_id": CHANGE_ID}}, CONTEXT)
    assert devops.devops.calls == [] and devops.github.calls == []


def test_an_expired_pending_check_is_gone(devops):
    devops.table.items[AZURE_CORRELATION] = {"correlation_id": {"S": AZURE_CORRELATION}, "check": {"S": json.dumps(CHECK)},
                                             "expires_at": {"N": str(int(time.time()) - 1)}}
    assert bridge.PendingChecks().get(AZURE_CORRELATION) is None
