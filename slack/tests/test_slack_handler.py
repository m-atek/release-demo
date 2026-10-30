"""The Slack handler, without AWS, Slack or Jenkins: every call is a fake.

    .venv/bin/python -m pytest -q slack/tests
"""

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("slack_handler", ROOT / "slack" / "handler.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

SECRET = "test-signing-secret, not a real one"
SAM, MATEUSZ = "U0SAM000001", "U0MATEUSZ01"
RESPONSE_URL = "https://hooks.slack.com/commands/T000/1/abc"
CONTEXT = SimpleNamespace(invoked_function_arn="arn:aws:lambda:eu-west-1:123456789012:function:release-demo-slack-handler")
SSM_VALUES = {
    "slack/signing-secret": SECRET,
    "slack/member-id/sam": SAM,
    "slack/bot-token": "xoxb-test-bot-token",
    "cloudflare/slack-handler-client-id": "test-client-id.access",
    "cloudflare/slack-handler-client-secret": "test-client-secret",
    "jenkins/release-bot-api-token": "test-api-token",
}


def services_config():
    """What infra/slack_handler.tf writes into config.json, from the real services.yaml."""
    services = yaml.safe_load((ROOT / "services.yaml").read_text())
    return {"services": {name: {"jira_project": s["jira"]["project"], "options": s.get("slack", {}).get("options", [])}
                         for name, s in services.items()}}


class FakeSSM:
    def __init__(self, values):
        self.values, self.calls = dict(values), 0

    def get_parameters(self, Names, WithDecryption):
        assert WithDecryption and len(Names) <= 10
        self.calls += 1
        return {"Parameters": [{"Name": n, "Value": self.values[n.removeprefix(app.SSM_PREFIX)]}
                               for n in Names if n.removeprefix(app.SSM_PREFIX) in self.values]}


class FakeLambda:
    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)


class FakeEC2:
    def __init__(self, state):
        self.state = state

    def describe_instances(self, InstanceIds):
        assert InstanceIds == ["i-0tools"]
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}


@pytest.fixture
def aws(monkeypatch):
    fakes = SimpleNamespace(ssm=FakeSSM(SSM_VALUES), lambda_=FakeLambda(), ec2=FakeEC2("running"))
    monkeypatch.setattr(app, "aws", lambda name: {"ssm": fakes.ssm, "lambda": fakes.lambda_, "ec2": fakes.ec2}[name])
    monkeypatch.setattr(app, "_parameters", {})
    monkeypatch.setattr(app, "config", services_config)
    monkeypatch.setenv("RELEASERS", "sam")
    monkeypatch.setenv("TOOLS_HOST_ID", "i-0tools")
    monkeypatch.setenv("JENKINS_URL", "https://jenkins-demo.es.nl")
    return fakes


def signed(text, command="/release", user=SAM, timestamp=None, secret=SECRET, encode=False):
    """A slash command as a Lambda function URL hands it over, signed like Slack signs it."""
    body = urllib.parse.urlencode({"command": command, "text": text, "user_id": user, "channel_id": "C0RELEASES",
                                   "response_url": RESPONSE_URL, "team_id": "T000"})
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"headers": {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature,
                        "Content-Type": "application/x-www-form-urlencoded"},
            "body": base64.b64encode(body.encode()).decode() if encode else body, "isBase64Encoded": encode}


def handed_over(fakes):
    assert len(fakes.lambda_.invocations) == 1
    call = fakes.lambda_.invocations[0]
    assert call["FunctionName"] == CONTEXT.invoked_function_arn and call["InvocationType"] == "Event"
    return json.loads(call["Payload"])["work"]


def ephemeral(response):
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["response_type"] == "ephemeral"
    return body["text"]


# Slack's signature

def test_a_signed_release_from_a_releaser_is_handed_over(aws):
    event = signed("orders-service ORD-12")
    response = app.handler(event, CONTEXT)
    assert response == {"statusCode": 200, "body": ""}
    work = handed_over(aws)
    assert work == {"job": "release", "channel": "C0RELEASES", "response_url": RESPONSE_URL,
                    "params": {"SERVICE": "orders-service", "JIRA_KEY": "ORD-12", "OPTION": "", "REQUESTED_BY": SAM,
                               "REQUESTED_AT": event["headers"]["X-Slack-Request-Timestamp"]}}


def test_a_base64_body_is_checked_the_same_way(aws):
    assert app.handler(signed("orders-service ORD-12", encode=True), CONTEXT)["statusCode"] == 200
    assert handed_over(aws)["params"]["JIRA_KEY"] == "ORD-12"


def test_a_wrong_signature_is_refused(aws):
    response = app.handler(signed("orders-service ORD-12", secret="f" * 32), CONTEXT)
    assert response["statusCode"] == 401
    assert aws.lambda_.invocations == []


def test_a_body_changed_after_signing_is_refused(aws):
    event = signed("orders-service ORD-12")
    event["body"] = event["body"].replace("ORD-12", "ORD-13")
    assert app.handler(event, CONTEXT)["statusCode"] == 401
    assert aws.lambda_.invocations == []


def test_a_request_without_signature_is_refused(aws):
    event = signed("orders-service ORD-12")
    del event["headers"]["X-Slack-Signature"]
    assert app.handler(event, CONTEXT)["statusCode"] == 401


def test_without_a_signing_secret_nothing_is_accepted(aws):
    aws.ssm.values["slack/signing-secret"] = "unset"
    assert app.handler(signed("orders-service ORD-12"), CONTEXT)["statusCode"] == 500
    assert aws.lambda_.invocations == []


def test_the_signature_check_matches_slacks_documented_example():
    # The example from Slack's "Verifying requests from Slack" page.
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V&"
            b"channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text=&"
            b"response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&"
            b"trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
    headers = {"x-slack-request-timestamp": "1531420618",
               "x-slack-signature": "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"}
    assert app.verify("8f742231b10e8888abcd99yyyzzz85a5", headers, body, now=1531420618) is None


# Age: five minutes at most, either way

# The exact edge, 300 seconds, is in the next test, with the clock fixed.
@pytest.mark.parametrize("age, accepted", [(0, True), (298, True), (302, False), (3600, False), (-302, False)])
def test_requests_older_than_five_minutes_are_refused(aws, age, accepted):
    response = app.handler(signed("orders-service ORD-12", timestamp=int(time.time()) - age), CONTEXT)
    assert response["statusCode"] == (200 if accepted else 401)
    assert len(aws.lambda_.invocations) == (1 if accepted else 0)


def test_the_age_check_comes_with_its_reason():
    body = b"command=%2Frelease"
    timestamp = "1793000000"
    signature = "v0=" + hmac.new(SECRET.encode(), b"v0:1793000000:" + body, hashlib.sha256).hexdigest()
    headers = {"x-slack-request-timestamp": timestamp, "x-slack-signature": signature}
    assert app.verify(SECRET, headers, body, now=1793000000 + 300) is None
    assert app.verify(SECRET, headers, body, now=1793000000 + 301) == "older than five minutes"
    assert app.verify(SECRET, {**headers, "x-slack-request-timestamp": "soon"}, body) == "no Slack signature"


# Only releasers

def test_someone_who_is_not_a_releaser_gets_the_private_refusal(aws):
    response = app.handler(signed("orders-service ORD-12", user=MATEUSZ), CONTEXT)
    assert ephemeral(response) == "You can't start releases here."
    assert aws.lambda_.invocations == []


def test_a_releaser_whose_member_id_is_unset_is_refused(aws):
    aws.ssm.values["slack/member-id/sam"] = "unset"
    assert ephemeral(app.handler(signed("orders-service ORD-12"), CONTEXT)) == "You can't start releases here."


def test_rollbacks_are_for_releasers_only_too(aws):
    response = app.handler(signed("orders-service", command="/rollback", user=MATEUSZ), CONTEXT)
    assert ephemeral(response) == "You can't start releases here."
    assert aws.lambda_.invocations == []


def test_ssm_is_asked_once_while_the_function_is_warm(aws):
    app.handler(signed("orders-service ORD-12"), CONTEXT)
    app.handler(signed("orders-service ORD-13"), CONTEXT)
    assert aws.ssm.calls == 1


# Parsing /release and /rollback against services.yaml

@pytest.mark.parametrize("command, text, job, params", [
    ("/release", "orders-service ORD-12", "release", {"SERVICE": "orders-service", "JIRA_KEY": "ORD-12", "OPTION": ""}),
    ("/release", "orders-service ORD-13 standard", "release",
     {"SERVICE": "orders-service", "JIRA_KEY": "ORD-13", "OPTION": "standard"}),
    ("/release", "  Orders-Service   ord-13  STANDARD ", "release",
     {"SERVICE": "orders-service", "JIRA_KEY": "ORD-13", "OPTION": "standard"}),
    ("/rollback", "orders-service", "rollback", {"SERVICE": "orders-service"}),
])
def test_commands_parse_into_a_job_and_its_parameters(command, text, job, params):
    assert app.parse(command, text, services_config()["services"]) == (job, params)


@pytest.mark.parametrize("command, text, says", [
    ("/release", "", "Usage: /release <service> <Jira key> [option]"),
    ("/release", "orders-service", "Usage: /release"),
    ("/release", "orders-service ORD-12 standard now", "Usage: /release"),
    ("/release", "billing ORD-12", "There's no service billing. Services: "),
    ("/release", "orders-service ABC-12", "ABC-12 isn't a Jira key for orders-service: it takes ORD-<number>"),
    ("/release", "orders-service ORD-0", "isn't a Jira key"),
    ("/release", "orders-service 12", "isn't a Jira key"),
    ("/release", "orders-service ORD-12x", "isn't a Jira key"),
    ("/release", "orders-service ORD-12 jamf", "orders-service has no option jamf. Its options: standard."),
    ("/rollback", "", "Usage: /rollback <service>"),
    ("/rollback", "orders-service now", "Usage: /rollback <service>"),
    ("/rollback", "billing", "There's no service billing."),
    ("/deploy", "orders-service", "This app knows /release, /rollback and /agent, not /deploy."),
])
def test_wrong_commands_get_a_private_hint(aws, command, text, says):
    with pytest.raises(app.Refusal, match=re.escape(says)):
        app.parse(command, text, services_config()["services"])
    assert says in ephemeral(app.handler(signed(text, command=command), CONTEXT))
    assert aws.lambda_.invocations == []


def test_a_rollback_is_handed_over_with_the_commands_time(aws):
    event = signed("orders-service", command="/rollback")
    assert app.handler(event, CONTEXT)["statusCode"] == 200
    work = handed_over(aws)
    assert work["job"] == "rollback"
    assert work["params"] == {"SERVICE": "orders-service", "REQUESTED_BY": SAM,
                              "REQUESTED_AT": event["headers"]["X-Slack-Request-Timestamp"]}


def test_slacks_certificate_check_gets_a_plain_ok(aws):
    assert app.handler({"headers": {}, "body": "ssl_check=1&token=x"}, CONTEXT) == {"statusCode": 200, "body": ""}


# The asynchronous call: tools host, status message, Jenkins

class Response(SimpleNamespace):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Calls:
    """post_json and the Jenkins opener, faked: Slack answers ok unless told otherwise."""

    def __init__(self, jenkins=(201,), slack_reply=None):
        self.posts, self.jenkins, self.jenkins_answers = [], [], list(jenkins)
        self.slack_reply = slack_reply or {"ok": True, "channel": "C0RELEASES", "ts": "1793000000.000100"}

    def post_json(self, url, body, headers=None):
        self.posts.append((url, body, headers or {}))
        return self.slack_reply if url.startswith("https://slack.com/api/") else {"ok": True}

    def open(self, request, timeout):
        self.jenkins.append(request)
        status = self.jenkins_answers.pop(0)
        if status != 201:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b""))
        return Response(status=201, headers={"Location": "https://jenkins-demo.es.nl/queue/item/7/"})

    def slack_calls(self, method):
        return [(body, headers) for url, body, headers in self.posts if url == f"https://slack.com/api/{method}"]

    def replies(self):
        return [body["text"] for url, body, _ in self.posts if url == RESPONSE_URL]


@pytest.fixture
def calls(monkeypatch):
    fake = Calls()
    monkeypatch.setattr(app, "post_json", fake.post_json)
    monkeypatch.setattr(app, "OPENER", SimpleNamespace(open=fake.open))
    return fake


def release_work(**params):
    return {"job": "release", "channel": "C0RELEASES", "response_url": RESPONSE_URL,
            "params": {"SERVICE": "orders-service", "JIRA_KEY": "ORD-12", "OPTION": "", "REQUESTED_BY": SAM,
                       "REQUESTED_AT": "1793000000", **params}}


@pytest.mark.parametrize("state, says", [
    ("stopped", "The release server is stopped. Start it with scripts/tools.sh up."),
    ("stopping", "The release server is stopped. Start it with scripts/tools.sh up."),
    ("pending", "The release server is starting. Try again when scripts/tools.sh up says Ready."),
])
def test_a_stopped_tools_host_gets_one_line_and_nothing_starts(aws, calls, state, says):
    aws.ec2.state = state
    app.handler({"work": release_work()}, CONTEXT)
    assert calls.replies() == [says]
    assert calls.slack_calls("chat.postMessage") == [] and calls.jenkins == []


def test_the_status_message_comes_first_then_jenkins_as_release_bot_through_access(aws, calls):
    app.handler({"work": release_work()}, CONTEXT)

    (message, slack_headers), = calls.slack_calls("chat.postMessage")
    assert slack_headers["Authorization"] == "Bearer xoxb-test-bot-token"
    assert message["channel"] == "C0RELEASES"
    assert message["text"].startswith(f"*Release orders-service* · ORD-12 · requested by <@{SAM}>")

    request, = calls.jenkins
    assert request.full_url == "https://jenkins-demo.es.nl/job/release/buildWithParameters?delay=0sec"
    assert request.get_method() == "POST"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["cf-access-client-id"] == "test-client-id.access"
    assert headers["cf-access-client-secret"] == "test-client-secret"
    assert headers["authorization"] == "Basic " + base64.b64encode(b"release-bot:test-api-token").decode()
    assert headers["user-agent"] == app.USER_AGENT and not headers["user-agent"].startswith("Python-urllib")
    assert {k: v[0] for k, v in urllib.parse.parse_qs(request.data.decode(), keep_blank_values=True).items()} == {
        "SERVICE": "orders-service", "JIRA_KEY": "ORD-12", "OPTION": "", "REQUESTED_BY": SAM,
        "REQUESTED_AT": "1793000000", "SLACK_CHANNEL": "C0RELEASES", "SLACK_TS": "1793000000.000100"}
    assert calls.slack_calls("chat.update") == [] and calls.replies() == []


def test_a_rollback_starts_the_rollback_job_with_the_same_slack_and_time_parameters(aws, calls):
    work = {"job": "rollback", "channel": "C0RELEASES", "response_url": RESPONSE_URL,
            "params": {"SERVICE": "orders-service", "REQUESTED_BY": SAM, "REQUESTED_AT": "1793000000"}}
    app.handler({"work": work}, CONTEXT)
    (message, _), = calls.slack_calls("chat.postMessage")
    assert message["text"].startswith(f"*Rollback orders-service* · production · requested by <@{SAM}>")
    request, = calls.jenkins
    assert request.full_url == "https://jenkins-demo.es.nl/job/rollback/buildWithParameters?delay=0sec"
    assert dict(urllib.parse.parse_qsl(request.data.decode())) == {
        "SERVICE": "orders-service", "REQUESTED_BY": SAM, "REQUESTED_AT": "1793000000",
        "SLACK_CHANNEL": "C0RELEASES", "SLACK_TS": "1793000000.000100"}


def test_jenkins_not_answering_turns_the_message_into_the_reason(aws, calls):
    calls.jenkins_answers = [502]
    app.handler({"work": release_work()}, CONTEXT)
    (update, _), = calls.slack_calls("chat.update")
    assert update["ts"] == "1793000000.000100"
    assert ":x:  Not started: Jenkins doesn't answer (502)" in update["text"]


def test_a_stale_release_bot_token_is_read_again_once(aws, calls):
    """The host makes a new token when the stored one stops working; a warm
    function may still hold the old one."""
    app._parameters["jenkins/release-bot-api-token"] = ("old-api-token", time.time() + 300)
    aws.ssm.values["jenkins/release-bot-api-token"] = "new-api-token"
    calls.jenkins_answers = [401, 201]
    app.handler({"work": release_work()}, CONTEXT)
    first, second = calls.jenkins
    assert first.get_header("Authorization") == "Basic " + base64.b64encode(b"release-bot:old-api-token").decode()
    assert second.get_header("Authorization") == "Basic " + base64.b64encode(b"release-bot:new-api-token").decode()
    assert calls.slack_calls("chat.update") == []


def test_a_channel_the_bot_is_not_in_gets_a_hint_and_nothing_starts(aws, calls):
    calls.slack_reply = {"ok": False, "error": "not_in_channel"}
    app.handler({"work": release_work()}, CONTEXT)
    assert calls.replies() == ["I can't post in this channel (not_in_channel). "
                               "Invite me with /invite @Release Bot, or use #releases."]
    assert calls.jenkins == []


def test_a_missing_token_is_named_and_nothing_starts(aws, calls):
    aws.ssm.values["jenkins/release-bot-api-token"] = "unset"
    app.handler({"work": release_work()}, CONTEXT)
    assert calls.replies() == ["The release server isn't set up yet: jenkins/release-bot-api-token isn't in SSM."]
    assert calls.slack_calls("chat.postMessage") == [] and calls.jenkins == []


def test_an_error_in_the_slow_part_is_reported_not_raised(aws, calls):
    aws.ec2.describe_instances = lambda InstanceIds: (_ for _ in ()).throw(RuntimeError("no"))
    assert app.handler({"work": release_work()}, CONTEXT) is None
    assert calls.replies() == ["The release didn't start (RuntimeError). The Slack handler's log in CloudWatch says why."]


def test_a_reply_goes_to_slack_only():
    sent = []
    original, app.post_json = app.post_json, lambda url, body, headers=None: sent.append(url)
    try:
        app.say("https://example.com/collect", "hello")
        app.say(RESPONSE_URL, "hello")
    finally:
        app.post_json = original
    assert sent == [RESPONSE_URL]


def test_an_access_redirect_is_not_followed(monkeypatch):
    """Cloudflare Access answers a bad service token with a redirect to its login
    page; followed, that would look like a started build."""

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
        status, _ = app.start_job(SSM_VALUES, "release", {"SERVICE": "orders-service"})
    finally:
        server.shutdown()
    assert status == 302
    assert "refused release-bot (302)" in app.jenkins_problem(status)
