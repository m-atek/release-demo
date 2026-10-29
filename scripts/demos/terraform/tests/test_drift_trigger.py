"""The drift trigger, without AWS or Jenkins: every call is a fake.

    .venv/bin/python -m pytest -q scripts/demos/terraform/tests
"""

import base64
import datetime
import io
import json
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import drift_trigger as app

ACCOUNT = "123456789012"
APP_GROUP = "sg-0a1b2c3d4e5f60718"
DEFAULT_GROUP = "sg-0f1e2d3c4b5a69788"
OTHER_GROUP = "sg-0aaaabbbbccccdddd"
BUCKET = "release-demo-store-exports-20261029abcdef"
ADMIN = "AWSReservedSSO_AdministratorAccess_0123456789abcdef"
PLAN_ROLE, APPLY_ROLE = "release-demo-deploy-network-plan", "release-demo-deploy-network-apply"
MADE = "2026-10-29T16:54:24Z"
SSM_VALUES = {
    "cloudflare/slack-handler-client-id": "test-client-id.access",
    "cloudflare/slack-handler-client-secret": "test-client-secret",
    "jenkins/release-bot-api-token": "test-api-token",
}


def millis(text):
    return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def cloudtrail(name="AuthorizeSecurityGroupIngress", role=ADMIN, session="mateusz", time=MADE, console=True,
               parameters=None, error=None):
    """A call as EventBridge hands it over: "AWS API Call via CloudTrail", with CloudTrail's record as detail."""
    bucket_call = name in app.BUCKET_CALLS
    detail = {
        "eventVersion": "1.10", "eventTime": time, "eventName": name, "awsRegion": "eu-west-1",
        "eventSource": "s3.amazonaws.com" if bucket_call else "ec2.amazonaws.com",
        "sourceIPAddress": "192.0.2.10",
        "userAgent": "Mozilla/5.0 (Macintosh)" if console else "aws-cli/2.38.6",
        "userIdentity": {
            "type": "AssumedRole", "principalId": f"AROAEXAMPLEROLEID:{session}", "accountId": ACCOUNT,
            "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{role}/{session}",
            "sessionContext": {"sessionIssuer": {"type": "Role", "principalId": "AROAEXAMPLEROLEID",
                                                 "arn": f"arn:aws:iam::{ACCOUNT}:role/{role}",
                                                 "accountId": ACCOUNT, "userName": role}},
        },
        "requestParameters": parameters if parameters is not None else (
            {"bucketName": BUCKET, "Host": f"{BUCKET}.s3.eu-west-1.amazonaws.com", "policy": ""} if bucket_call else
            {"groupId": APP_GROUP, "ipPermissions": {"items": [{
                "ipProtocol": "tcp", "fromPort": 22, "toPort": 22, "groups": {},
                "ipRanges": {"items": [{"cidrIp": "0.0.0.0/0"}]}, "ipv6Ranges": {}, "prefixListIds": {}}]}}),
        "eventType": "AwsApiCall", "managementEvent": True, "readOnly": False, "recipientAccountId": ACCOUNT,
    }
    if console:
        detail["sessionCredentialFromConsole"] = "true"
    if error:
        detail["errorCode"] = error
    return {"version": "0", "detail-type": "AWS API Call via CloudTrail", "source": "aws.s3" if bucket_call else "aws.ec2",
            "account": ACCOUNT, "time": time, "region": "eu-west-1", "resources": [], "detail": detail}


class FakeEC2:
    def __init__(self):
        self.state = "running"
        self.groups = {
            APP_GROUP: [{"Key": "Name", "Value": "network-stack-app"}, {"Key": "Demo", "Value": "2-terraform"}],
            DEFAULT_GROUP: [{"Key": "Name", "Value": "network-stack-default-unused"},
                            {"Key": "Demo", "Value": "2-terraform"}],
            OTHER_GROUP: [{"Key": "Name", "Value": "release-demo-tools-host"}, {"Key": "Project", "Value": "release-demo"}],
        }

    def describe_instances(self, Filters):
        assert {"Name": "tag:Name", "Values": ["release-demo-tools-host"]} in Filters
        instances = [{"State": {"Name": self.state}}] if self.state else []
        return {"Reservations": [{"Instances": instances}] if instances else []}

    def describe_security_groups(self, GroupIds):
        (group,) = GroupIds
        if group not in self.groups:
            raise ClientError({"Error": {"Code": "InvalidGroup.NotFound", "Message": "not found"}},
                              "DescribeSecurityGroups")
        return {"SecurityGroups": [{"GroupId": group, "Tags": self.groups[group]}]}


class FakeSSM:
    def __init__(self, values):
        self.values, self.calls = dict(values), 0

    def get_parameters(self, Names, WithDecryption):
        assert WithDecryption and len(Names) <= 10
        self.calls += 1
        return {"Parameters": [{"Name": n, "Value": self.values[n.removeprefix(app.SSM_PREFIX)]}
                               for n in Names if n.removeprefix(app.SSM_PREFIX) in self.values]}


class Answer(SimpleNamespace):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class FakeJenkins:
    """Jenkins behind Cloudflare Access, as far as the function asks: network-drift's
    place in the queue and its last build. Records every request."""

    def __init__(self):
        self.requests, self.queue, self.next_id = [], [], 41
        self.last_build = {"number": 8, "timestamp": millis("2026-10-29T07:00:00Z")}
        self.answers = []  # statuses to answer first, whatever is asked
        self.stale_checks = 0  # job checks answered as if nothing were queued: two changes at once

    def open(self, request, timeout):
        self.requests.append(request)
        if self.answers:
            status = self.answers.pop(0)
            if status == 0:
                raise urllib.error.URLError("timed out")
            raise urllib.error.HTTPError(request.full_url, status, "refused", {}, io.BytesIO(b""))
        url = urllib.parse.urlsplit(request.full_url)
        method, path = request.get_method(), url.path
        if (method, path) == ("GET", "/job/network-drift/api/json"):
            stale = self.stale_checks > 0
            self.stale_checks -= 1 if stale else 0
            job = {"inQueue": bool(self.queue) and not stale, "lastBuild": self.last_build}
            return Answer(status=200, headers={}, body=json.dumps(job).encode())
        if (method, path) == ("POST", "/job/network-drift/buildWithParameters"):
            self.next_id += 1
            self.queue.append({"id": self.next_id, "params": dict(urllib.parse.parse_qsl(request.data.decode()))})
            return Answer(status=201, headers={"Location": f"https://jenkins-demo.es.nl/queue/item/{self.next_id}/"},
                          body=b"")
        if (method, path) == ("GET", "/queue/api/json"):
            # Another job's item, older than all of them, must not count.
            items = [{"id": 3, "task": {"name": "release"}}] + [
                {"id": item["id"], "task": {"name": "network-drift"}} for item in self.queue]
            return Answer(status=200, headers={}, body=json.dumps({"items": items}).encode())
        if (method, path) == ("POST", "/queue/cancelItem"):
            wanted = int(urllib.parse.parse_qs(url.query)["id"][0])
            self.queue = [item for item in self.queue if item["id"] != wanted]
            return Answer(status=204, headers={}, body=b"")
        raise AssertionError(f"unexpected {method} {path}")

    def started(self):
        return [r for r in self.requests if urllib.parse.urlsplit(r.full_url).path.endswith("/buildWithParameters")]

    def cancelled(self):
        return [r.full_url for r in self.requests if "/queue/cancelItem" in r.full_url]


@pytest.fixture
def aws(monkeypatch):
    fakes = SimpleNamespace(ec2=FakeEC2(), ssm=FakeSSM(SSM_VALUES), jenkins=FakeJenkins(), log=[])
    monkeypatch.setattr(app, "aws", lambda name: {"ec2": fakes.ec2, "ssm": fakes.ssm}[name])
    monkeypatch.setattr(app, "_parameters", {})
    monkeypatch.setattr(app, "OPENER", SimpleNamespace(open=fakes.jenkins.open))
    monkeypatch.setattr(app, "log", fakes.log.append)
    monkeypatch.setenv("JENKINS_URL", "https://jenkins-demo.es.nl")
    monkeypatch.setenv("IGNORED_ROLES", f"{PLAN_ROLE},{APPLY_ROLE}")
    monkeypatch.setenv("DEMO_TAG", "2-terraform")
    monkeypatch.setenv("BUCKET_PREFIX", "release-demo-store-exports-")
    return fakes


def queued_params(fakes):
    (request,) = fakes.jenkins.started()
    return dict(urllib.parse.parse_qsl(request.data.decode()))


# What starts a run

def test_a_rule_added_in_the_console_queues_network_drift_with_the_change(aws):
    app.handler(cloudtrail(), None)
    (request,) = aws.jenkins.started()
    assert request.full_url == "https://jenkins-demo.es.nl/job/network-drift/buildWithParameters?delay=60sec"
    assert request.get_method() == "POST"
    assert queued_params(aws) == {"TRIGGER": "event", "EVENT_NAME": "AuthorizeSecurityGroupIngress",
                                  "EVENT_TIME": MADE, "RESOURCE_ID": APP_GROUP, "FROM_CONSOLE": "true"}
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["cf-access-client-id"] == "test-client-id.access"
    assert headers["cf-access-client-secret"] == "test-client-secret"
    assert headers["authorization"] == "Basic " + base64.b64encode(b"release-bot:test-api-token").decode()
    assert headers["user-agent"] == app.USER_AGENT and not headers["user-agent"].startswith("Python-urllib")
    assert aws.log[-1] == (f"AuthorizeSecurityGroupIngress on {APP_GROUP} at {MADE}: "
                           "network-drift queued as item 42, starting in 60 s")


def test_no_person_and_no_account_reaches_jenkins(aws):
    app.handler(cloudtrail(), None)
    values = " ".join(queued_params(aws).values())
    assert ACCOUNT not in values and "mateusz" not in values and ADMIN not in values


def test_a_change_through_the_api_says_so(aws):
    app.handler(cloudtrail(console=False), None)
    assert queued_params(aws)["FROM_CONSOLE"] == "false"


@pytest.mark.parametrize("name", sorted(app.GROUP_CALLS))
def test_every_rule_call_on_the_stacks_groups_starts_a_run(aws, name):
    app.handler(cloudtrail(name=name), None)
    assert queued_params(aws)["EVENT_NAME"] == name


def test_the_default_group_is_the_stacks_too(aws):
    app.handler(cloudtrail(name="RevokeSecurityGroupEgress", parameters={
        "groupId": DEFAULT_GROUP, "ipPermissions": {}, "securityGroupRuleIds": {"items": [
            {"securityGroupRuleId": "sgr-0123456789abcdef0"}]}}), None)
    assert queued_params(aws)["RESOURCE_ID"] == DEFAULT_GROUP


def test_modify_security_group_rules_names_its_group_inside_the_request(aws):
    parameters = {"ModifySecurityGroupRulesRequest": {"GroupId": APP_GROUP, "SecurityGroupRule": {
        "tag": 1, "SecurityGroupRuleId": "sgr-0123456789abcdef0", "SecurityGroupRule": {
            "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "ReferencedGroupId": OTHER_GROUP}}}}
    app.handler(cloudtrail(name="ModifySecurityGroupRules", parameters=parameters), None)
    assert queued_params(aws)["RESOURCE_ID"] == APP_GROUP


def test_a_rule_that_refers_to_another_group_is_about_the_group_it_is_on(aws):
    parameters = {"groupId": OTHER_GROUP, "ipPermissions": {"items": [{"ipProtocol": "tcp", "fromPort": 8082,
                                                                       "toPort": 8082, "groups": {"items": [
                                                                           {"groupId": APP_GROUP}]}}]}}
    app.handler(cloudtrail(parameters=parameters), None)
    assert aws.jenkins.requests == []


@pytest.mark.parametrize("name", sorted(app.BUCKET_CALLS))
def test_policy_and_public_access_changes_on_the_exports_bucket_start_a_run(aws, name):
    app.handler(cloudtrail(name=name), None)
    assert queued_params(aws)["EVENT_NAME"] == name
    assert queued_params(aws)["RESOURCE_ID"] == BUCKET


# What starts nothing

@pytest.mark.parametrize("role, session", [
    (PLAN_ROLE, "network-drift-12"),
    (APPLY_ROLE, "network-stack-PR-2-5"),  # a pull request's apply
    (APPLY_ROLE, "reset-7"),  # reset.py terraform, from the Jenkins job reset
    (APPLY_ROLE, "mac-local"),  # reset.sh or stack.py up, from the Mac
])
def test_the_stacks_own_terraform_roles_start_nothing(aws, role, session):
    app.handler(cloudtrail(name="RevokeSecurityGroupIngress", role=role, session=session, console=False), None)
    app.handler(cloudtrail(name="PutBucketPublicAccessBlock", role=role, session=session, console=False), None)
    assert aws.jenkins.requests == []
    assert aws.log == [f"ignored: RevokeSecurityGroupIngress by {role}: the network stack's own Terraform role",
                       f"ignored: PutBucketPublicAccessBlock by {role}: the network stack's own Terraform role"]


def test_the_role_comes_from_the_session_arn_when_the_issuer_is_missing(aws):
    event = cloudtrail(role=APPLY_ROLE)
    del event["detail"]["userIdentity"]["sessionContext"]
    app.handler(event, None)
    assert aws.jenkins.requests == []


@pytest.mark.parametrize("event", [
    cloudtrail(parameters={"groupId": OTHER_GROUP, "ipPermissions": {}}),
    cloudtrail(parameters={"groupId": "sg-0deleted000000000", "ipPermissions": {}}),
    cloudtrail(parameters={}),
    cloudtrail(name="PutBucketPolicy", parameters={"bucketName": "release-demo-tfstate-0123abcd"}),
    cloudtrail(name="CreateTags"),
    cloudtrail(error="InvalidPermission.Duplicate"),
], ids=["another group", "a group that is gone", "no group", "another bucket", "a call it doesn't watch",
        "a call that failed"])
def test_changes_elsewhere_start_nothing(aws, event):
    app.handler(event, None)
    assert aws.jenkins.requests == []
    assert len(aws.log) == 1 and aws.log[0].startswith("ignored: ")
    assert ACCOUNT not in aws.log[0]


@pytest.mark.parametrize("state, says", [("stopped", "stopped"), ("stopping", "stopping"), ("pending", "pending"),
                                         (None, "not there")])
def test_a_stopped_tools_host_starts_nothing_and_leaves_it_to_the_0700_run(aws, state, says):
    aws.ec2.state = state
    app.handler(cloudtrail(), None)
    assert aws.jenkins.requests == []
    assert aws.log == [f"AuthorizeSecurityGroupIngress on {APP_GROUP} at {MADE}: the tools host is {says}, "
                       "so nothing started. The 07:00 run will find it."]


# Several changes within a minute start one run

def test_a_change_while_a_run_waits_in_the_queue_is_in_that_run(aws):
    app.handler(cloudtrail(), None)
    app.handler(cloudtrail(name="ModifySecurityGroupRules", time="2026-10-29T16:54:40Z", parameters={
        "ModifySecurityGroupRulesRequest": {"GroupId": APP_GROUP, "SecurityGroupRule": {}}}), None)
    app.handler(cloudtrail(name="RevokeSecurityGroupIngress", time="2026-10-29T16:55:10Z"), None)
    assert len(aws.jenkins.started()) == 1
    assert queued_params(aws)["EVENT_NAME"] == "AuthorizeSecurityGroupIngress"
    assert aws.log[1:] == [
        f"ModifySecurityGroupRules on {APP_GROUP} at 2026-10-29T16:54:40Z: in the network-drift run waiting in the queue",
        f"RevokeSecurityGroupIngress on {APP_GROUP} at 2026-10-29T16:55:10Z: in the network-drift run waiting in the queue"]


def test_a_change_made_before_the_last_run_started_is_in_that_run(aws):
    """CloudTrail can take a minute or two: the run another change started may have begun meanwhile."""
    aws.jenkins.last_build = {"number": 9, "timestamp": millis("2026-10-29T16:55:30Z")}
    app.handler(cloudtrail(time="2026-10-29T16:55:00Z"), None)
    assert aws.jenkins.started() == []
    assert aws.log == [f"AuthorizeSecurityGroupIngress on {APP_GROUP} at 2026-10-29T16:55:00Z: "
                       "in network-drift #9, which started after it"]


def test_a_change_after_the_last_run_started_gets_a_run_of_its_own(aws):
    aws.jenkins.last_build = {"number": 9, "timestamp": millis("2026-10-29T16:54:20Z")}
    app.handler(cloudtrail(), None)
    assert len(aws.jenkins.started()) == 1


def test_two_changes_at_the_same_moment_start_one_run(aws):
    """Both find nothing queued and both queue a run: the later queue item gives way."""
    aws.jenkins.stale_checks = 2
    app.handler(cloudtrail(), None)
    app.handler(cloudtrail(name="ModifySecurityGroupRules", parameters={
        "ModifySecurityGroupRulesRequest": {"GroupId": APP_GROUP, "SecurityGroupRule": {}}}), None)
    assert [item["id"] for item in aws.jenkins.queue] == [42]
    assert aws.jenkins.queue[0]["params"]["EVENT_NAME"] == "AuthorizeSecurityGroupIngress"
    assert aws.jenkins.cancelled() == ["https://jenkins-demo.es.nl/queue/cancelItem?id=43"]
    assert aws.log[-1] == f"ModifySecurityGroupRules on {APP_GROUP} at {MADE}: in queue item 42, queued a moment before"


# Jenkins and Cloudflare Access

def test_a_stale_release_bot_token_is_read_again_once(aws):
    """The host makes a new token when the stored one stops working; a warm
    function may still hold the old one."""
    app._parameters["jenkins/release-bot-api-token"] = ("old-api-token", 4102444800)
    aws.ssm.values["jenkins/release-bot-api-token"] = "new-api-token"
    aws.jenkins.answers = [401]
    app.handler(cloudtrail(), None)
    first, second = aws.jenkins.requests[:2]
    assert first.get_header("Authorization") == "Basic " + base64.b64encode(b"release-bot:old-api-token").decode()
    assert second.get_header("Authorization") == "Basic " + base64.b64encode(b"release-bot:new-api-token").decode()
    assert len(aws.jenkins.started()) == 1


@pytest.mark.parametrize("answers, says", [
    ([502, 502], "Jenkins answered 502."),
    ([0, 0], "Jenkins didn't answer (timed out)."),
    ([302, 302], "Jenkins or Cloudflare Access refused release-bot (302)"),
])
def test_jenkins_not_starting_the_run_is_logged(aws, answers, says):
    aws.jenkins.answers = answers
    app.handler(cloudtrail(), None)
    assert says in aws.log[-1] and aws.log[-1].endswith("Nothing started.")


def test_a_missing_token_is_named_and_nothing_starts(aws):
    aws.ssm.values["jenkins/release-bot-api-token"] = "unset"
    app.handler(cloudtrail(), None)
    assert aws.jenkins.requests == []
    assert aws.log[-1].endswith("not set in SSM: jenkins/release-bot-api-token. Nothing started.")


def test_an_error_is_logged_not_raised(aws):
    aws.ec2.describe_instances = lambda Filters: (_ for _ in ()).throw(RuntimeError("no"))
    assert app.handler(cloudtrail(), None) is None
    assert aws.log == ["failed: RuntimeError: no"]


def test_ssm_is_asked_once_while_the_function_is_warm(aws):
    app.handler(cloudtrail(), None)
    app.handler(cloudtrail(name="RevokeSecurityGroupIngress"), None)
    assert aws.ssm.calls == 1


def test_an_access_redirect_is_not_followed(monkeypatch):
    """Cloudflare Access answers a bad service token with a redirect to its login
    page; followed, that would look like a queued run."""

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "/cdn-cgi/access/login")
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"login page")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("JENKINS_URL", f"http://127.0.0.1:{server.server_port}")
        status, _, _ = app.call(SSM_VALUES, "POST", "/job/network-drift/buildWithParameters?delay=60sec", {})
    finally:
        server.shutdown()
    assert status == 302
