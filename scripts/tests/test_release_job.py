"""The release job client against a small fake Jenkins on localhost."""

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_job import ReleaseJob, version_of, with_parameters  # noqa: E402


class FakeJenkins(BaseHTTPRequestHandler):
    requests = []
    queue = []

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        FakeJenkins.requests.append(("GET", url.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"release-bot:secret").decode():
            return self.reply(401)
        if url.path == "/queue/item/42/api/json":
            return self.reply(200, json.dumps(FakeJenkins.queue.pop(0)).encode())
        if url.path == "/job/release/api/json":
            builds = [{"number": 9, "building": False, "result": "SUCCESS", "displayName": "#9 orders-service 1.2.0",
                       "actions": [{}, {"parameters": [{"name": "JIRA_KEY", "value": "ORD-1"}]}, None]}]
            return self.reply(200, json.dumps({"builds": builds}).encode())
        if url.path == "/job/release/9/wfapi/describe":
            return self.reply(200, json.dumps({"stages": [{"name": "Build", "status": "SUCCESS", "durationMillis": 12500}]}).encode())
        if url.path == "/job/release/9/consoleText":
            return self.reply(200, b"ERROR: Production check failed: rolled back to 1.2.3.\n")
        self.reply(404)

    def do_POST(self):
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode()) if length else {}
        FakeJenkins.requests.append(("POST", self.path, form))
        if url.path == "/job/release/buildWithParameters":
            return self.reply(201, headers={"Location": "https://jenkins-demo.example/queue/item/42/"})
        if url.path in ("/job/release/9/stop", "/queue/cancelItem"):
            return self.reply(302, headers={"Location": "/job/release/9/"})
        self.reply(404)

    def log_message(self, *args):
        pass


@pytest.fixture
def jenkins():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJenkins)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeJenkins.requests = []
    yield ReleaseJob("secret", base=f"http://127.0.0.1:{server.server_port}")
    server.shutdown()


def test_start_sends_the_slack_handlers_request_and_returns_the_queue_item(jenkins):
    params = {"SERVICE": "orders-service", "JIRA_KEY": "ORD-1", "OPTION": "standard",
              "REQUESTED_BY": "U0SAM", "SLACK_CHANNEL": "", "SLACK_TS": ""}
    assert jenkins.start(params) == 42
    method, path, form = FakeJenkins.requests[-1]
    assert (method, path) == ("POST", "/job/release/buildWithParameters?delay=0sec")
    assert form == {"SERVICE": ["orders-service"], "JIRA_KEY": ["ORD-1"], "OPTION": ["standard"], "REQUESTED_BY": ["U0SAM"]}


def test_started_waits_in_the_queue_then_returns_the_build_number(jenkins, monkeypatch):
    monkeypatch.setattr("release_job.time.sleep", lambda seconds: None)
    FakeJenkins.queue = [{"why": "Build #8 is already in progress"}, {"why": "still"}, {"executable": {"number": 9}}]
    told = []
    assert jenkins.started(42, waiting=told.append) == 9
    assert told == ["Build #8 is already in progress"]


def test_a_cancelled_queue_item_stops(jenkins, monkeypatch):
    monkeypatch.setattr("release_job.time.sleep", lambda seconds: None)
    FakeJenkins.queue = [{"cancelled": True}]
    with pytest.raises(SystemExit):
        jenkins.started(42)


def test_builds_stages_and_console(jenkins):
    build = jenkins.builds()[0]
    assert build["parameters"] == {"JIRA_KEY": "ORD-1"} and version_of(build) == "1.2.0"
    assert jenkins.stages(9) == [("Build", "SUCCESS", 12.5)]
    assert "rolled back to 1.2.3" in jenkins.console(9)


def test_stop_and_cancel_take_jenkins_redirect_as_done(jenkins):
    jenkins.stop(9)
    jenkins.cancel(5)
    assert [r[1] for r in FakeJenkins.requests[-2:]] == ["/job/release/9/stop", "/queue/cancelItem?id=5"]


def test_a_refused_token_says_what_to_do():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJenkins)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(SystemExit):
            ReleaseJob("wrong", base=f"http://127.0.0.1:{server.server_port}").builds()
    finally:
        server.shutdown()


def test_version_of_waits_for_the_run_to_name_itself():
    assert version_of({"displayName": "#12"}) is None
    assert version_of({"displayName": "#12 orders-service 1.2.8"}) == "1.2.8"
    assert with_parameters({"actions": None})["parameters"] == {}
