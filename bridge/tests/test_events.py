import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import events  # noqa: E402
import handler  # noqa: E402

EVENT = {
    "kind": "drift", "id": "catalog-production:2026-10-30T10:15:14Z", "application": "catalog-production",
    "service": "catalog", "namespace": "production", "resource": "deployment/catalog",
    "path": "deploy/production/catalog", "cmdb_item": "catalog-production", "revision": "abc1234def",
    "edits": [{"user": "pieter-oncall", "groups": ["release-demo:break-glass", "system:authenticated"],
               "at": "2026-10-30T10:15:07.123456Z", "verb": "patch", "field_manager": "kubectl-scale",
               "resource": "deployments/catalog", "request": {"spec": {"replicas": 5}}}],
    "diff": [{"path": "spec.replicas", "live": 5, "git": 3}],
    "out_of_sync_at": "2026-10-30T10:15:08.000000Z", "reverted_at": "2026-10-30T10:15:14.654321Z", "seconds": 7,
    "break_glass": True,
    "certificate": {"user": "pieter-oncall", "issued": "2026-10-30T10:05:00.000000Z",
                    "expires": "2026-10-30T11:05:00.000000Z"},
}
VALUES = {name: "x" for name in events.PARAMETERS}
VALUES.update({"bridge/events-token": "secret-token", "servicenow/instance-url": "https://sn.invalid",
               "grafana/stack-url": "https://grafana.invalid", "grafana/logs-url": "https://logs.invalid"})


def utc(*parts):
    return datetime.datetime(*parts, tzinfo=datetime.timezone.utc)


def test_amsterdam_time_follows_summer_time():
    assert events.amsterdam(utc(2026, 10, 30, 10, 15, 7)).strftime("%H:%M") == "11:15"  # CET, after 25 October
    assert events.amsterdam(utc(2026, 10, 24, 10, 15, 7)).strftime("%H:%M") == "12:15"  # CEST
    assert events.amsterdam(utc(2026, 3, 29, 0, 59)).strftime("%H:%M") == "01:59"
    assert events.amsterdam(utc(2026, 3, 29, 1, 0)).strftime("%H:%M") == "03:00"
    assert events.amsterdam(utc(2026, 10, 25, 0, 59)).strftime("%H:%M") == "02:59"
    assert events.amsterdam(utc(2026, 10, 25, 1, 0)).strftime("%H:%M") == "02:00"


def test_slack_says_what_the_script_quotes():
    assert events.slack_text(EVENT, "INC0010042").splitlines() == [
        "*Production drifted from Git* · catalog · INC0010042",
        "spec.replicas 5, set by pieter-oncall (break-glass) at 11:15:07.",
        "Argo CD put Git's 3 back at 11:15:14, after 7s.",
        "To keep a change, open a pull request to deploy/production/catalog.",
    ]


def test_the_work_note_holds_the_timeline():
    assert events.timeline(EVENT).splitlines() == [
        "11:15:07 spec.replicas set to 5 by pieter-oncall (group release-demo:break-glass), kubectl scale",
        "11:15:08 Argo CD: OutOfSync",
        "11:15:14 Argo CD self-heal: spec.replicas 3, from Git at abc1234",
        "Break-glass certificate for pieter-oncall issued 11:05:00, valid until 12:05:00",
        "To keep a change, open a pull request to deploy/production/catalog.",
    ]


def test_the_description_holds_the_diff_the_editor_and_three_times():
    text = events.description(EVENT)
    assert "Changed: spec.replicas 5, Git 3." in text
    assert "Edited by pieter-oncall (group release-demo:break-glass) with kubectl scale." in text
    assert "Hand edit: 11:15:07" in text and "OutOfSync: 11:15:08" in text
    assert "back: 11:15:14, after 7 s, from Git at abc1234." in text
    assert "30 October 2026, Europe/Amsterdam" in text


def test_a_user_who_isnt_break_glass_gets_no_label():
    plain = {**EVENT, "break_glass": False, "certificate": None,
             "edits": [{**EVENT["edits"][0], "user": "someone", "groups": ["system:authenticated"]}]}
    assert "set by someone at" in events.slack_text(plain, "INC1")
    assert "Break-glass" not in events.timeline(plain)


class FakeServiceNow:
    created, patched = [], []
    existing = None

    def __init__(self, url, user, password):
        pass

    def find(self, table, query, fields=None):
        if table == "incident":
            return self.existing
        return {"sys_id": f"{table}-id", "number": "x"}

    def call(self, method, path, body=None, **query):
        FakeServiceNow.created.append((method, path, body))
        return {"number": "INC0010042", "sys_id": "inc-id"}


@pytest.fixture
def wired(monkeypatch):
    FakeServiceNow.created, FakeServiceNow.existing = [], None
    sent = {"slack": [], "post": []}
    monkeypatch.setattr(events, "parameters", lambda *names: {n: VALUES.get(n) for n in names})
    monkeypatch.setattr(events, "ServiceNow", FakeServiceNow)
    monkeypatch.setattr(events, "slack", lambda token, method, **f: sent["slack"].append(f) or {"ok": True})
    monkeypatch.setattr(events, "send", lambda url, body, headers: sent["post"].append((url, body)) or 204)
    return sent


def request(body, token="secret-token"):
    return {"rawPath": "/events", "requestContext": {"http": {"method": "POST"}},
            "headers": {"Authorization": f"Bearer {token}"} if token else {}, "body": json.dumps(body)}


def test_a_drift_event_opens_one_incident_and_posts_once(wired):
    reply = handler.handler(request(EVENT), None)
    assert reply["statusCode"] == 200 and json.loads(reply["body"])["incident"] == "INC0010042"
    method, path, fields = FakeServiceNow.created[0]
    assert (method, path) == ("POST", "/api/now/table/incident")
    assert fields["short_description"] == "catalog in production changed outside Git; Argo CD put it back"
    assert (fields["category"], fields["impact"], fields["urgency"], fields["state"]) == ("software", "3", "3", "2")
    assert fields["correlation_id"] == "argocd-drift:catalog-production:2026-10-30T10:15:14Z"
    assert fields["cmdb_ci"] == "cmdb_ci-id" and fields["caller_id"] == "sys_user-id"
    assert len(wired["slack"]) == 1 and wired["slack"][0]["text"].startswith("*Production drifted from Git*")
    annotation = [body for url, body in wired["post"] if url.endswith("/api/annotations")][0]
    assert annotation["text"] == "catalog in production: hand edit put back by Argo CD"
    assert annotation["tags"] == ["release-demo", "drift", "catalog"]


def test_a_retried_event_finds_its_incident(wired):
    FakeServiceNow.existing = {"number": "INC0010042", "sys_id": "inc-id"}
    reply = handler.handler(request(EVENT), None)
    assert json.loads(reply["body"]) == {"incident": "INC0010042", "duplicate": True}
    assert FakeServiceNow.created == [] and wired["slack"] == []


def test_without_the_token_nothing_is_written(wired):
    assert handler.handler(request(EVENT, token="wrong"), None)["statusCode"] == 401
    assert handler.handler(request(EVENT, token=None), None)["statusCode"] == 401
    assert FakeServiceNow.created == []


def test_only_drift_and_only_post_events(wired):
    assert handler.handler(request({**EVENT, "kind": "deploy"}), None)["statusCode"] == 400
    # One function with the approval routes (demos 14 and 22): every route takes POST only.
    assert handler.handler({"rawPath": "/events", "requestContext": {"http": {"method": "GET"}}}, None)["statusCode"] == 405
    incomplete = {k: v for k, v in EVENT.items() if k != "diff"}
    assert handler.handler(request(incomplete), None)["statusCode"] == 400
