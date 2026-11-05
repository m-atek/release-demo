"""change.py's wait: only the version services.yaml names may wait hours, through a ServiceNow that sleeps, and
every rule about who may approve still holds. Any other version waits, and stops, as it always has."""

import datetime
import http.client
import sys
import urllib.error

import pytest

import change
import common
from conftest import ANY_OTHER, WAITS, Clock, FakeServiceNow, waiting_state

REQUESTED = {"approval": "requested", "state": "-4"}
APPROVED = {"approval": "approved", "state": "-2"}
ASLEEP = SystemExit("ServiceNow is asleep: wake it at developer.servicenow.com")


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(change.time, "time", clock.time)
    monkeypatch.setattr(change.time, "sleep", clock.sleep)
    monkeypatch.setattr(change, "agent_users", lambda sn: set())
    return clock


# Which version waits how long.

def test_services_yaml_names_one_version_for_ten_hours():
    settings = common.services()["orders-service"]["approval_wait"]
    assert settings == {"versions": [WAITS], "hours": 10}
    assert [name for name, config in common.services().items() if config.get("approval_wait")] == ["orders-service"]


def test_only_the_named_version_gets_the_long_limit():
    assert change.long_wait({"service": "orders-service", "version": WAITS})["hours"] == 10
    assert change.approval_limit({"service": "orders-service", "version": WAITS}) == 36000
    for state in ({"service": "orders-service", "version": ANY_OTHER}, {"service": "orders-service", "version": "1.4.0"},
                  {"service": "catalog", "version": WAITS}, {"service": "no-such-service", "version": WAITS}, {}):
        assert change.long_wait(state) is None
        assert change.approval_limit(state) == 1800


@pytest.mark.parametrize("argv, version, expected", [
    (["wait"], WAITS, 36000),
    (["wait"], ANY_OTHER, 1800),
    (["wait"], "1.4.0", 1800),
    (["wait", "--timeout", "60"], WAITS, 60),
    (["wait", "--timeout", "60"], "1.4.0", 60),
])
def test_the_command_passes_the_limit_on(monkeypatch, argv, version, expected):
    waited = []
    waiting_state(version)
    monkeypatch.setattr(sys, "argv", ["change.py", *argv])
    monkeypatch.setattr(change, "connect", lambda: (None, None))
    monkeypatch.setattr(change, "wait", lambda sn, state, timeout, interval: waited.append((timeout, interval)))
    change.main()
    assert waited == [(expected, 15)]


# The long wait.

def test_a_long_wait_outlasts_thirty_minutes_and_ends_on_the_approval(clock):
    state = waiting_state()
    four_hours_of_looks = [REQUESTED] * 960
    sn = FakeServiceNow([*four_hours_of_looks, APPROVED])
    change.wait(sn, state, change.approval_limit(state), 15)
    assert clock.slept == [15] * 960
    assert state["change"]["approved_by"] == "Alex"
    assert state["long_wait"] is True and change.run_state.load()["long_wait"] is True


@pytest.mark.parametrize("trouble", [
    ASLEEP,
    urllib.error.URLError("timed out"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
    http.client.RemoteDisconnected("closed"),
    http.client.IncompleteRead(b""),
    ValueError("Expecting value: an HTML page"),
    KeyError("result"),
])
def test_a_long_wait_outlasts_a_servicenow_that_doesn_t_answer(clock, capsys, trouble):
    state = waiting_state()
    sn = FakeServiceNow([REQUESTED, trouble, trouble, REQUESTED, APPROVED])
    change.wait(sn, state, change.approval_limit(state), 15)
    assert state["change"]["approved_by"] == "Alex"
    assert capsys.readouterr().out.count("ServiceNow didn't answer") == 2


def test_a_long_wait_outlasts_trouble_on_the_approvals_read_and_on_the_name(clock, monkeypatch):
    state = waiting_state()
    sn = FakeServiceNow([REQUESTED, APPROVED])
    real, troubles = sn.call, [ASLEEP]

    def call(method, path, body=None, **query):
        if path == "/api/now/table/sysapproval_approver" and troubles:
            raise troubles.pop()
        return real(method, path, body, **query)

    names, name_troubles = change.first_name, [urllib.error.URLError("no route")]

    def first_name(sn, sys_id):
        if name_troubles:
            raise name_troubles.pop()
        return names(sn, sys_id)

    monkeypatch.setattr(sn, "call", call)
    monkeypatch.setattr(change, "first_name", first_name)
    change.wait(sn, state, change.approval_limit(state), 15)
    assert state["change"]["approved_by"] == "Alex" and not troubles and not name_troubles


def test_a_long_wait_still_gives_up_at_its_own_limit(clock, monkeypatch):
    state = waiting_state()
    sn = FakeServiceNow([REQUESTED])
    cancelled = []
    monkeypatch.setattr(change, "cancel", lambda sn, state, notes: cancelled.append(notes))
    with pytest.raises(SystemExit) as stopped:
        change.wait(sn, state, change.approval_limit(state), 15)
    assert stopped.value.code == 1
    assert cancelled == ["No approval within 600 minutes. The release stopped before production."]
    assert clock.now - 1793000000.0 == pytest.approx(36000, abs=15)
    assert "long_wait" not in change.run_state.load()


@pytest.mark.parametrize("who, why", [("sam-id", "the person who asks"), ("pipeline-id", "only a person approves")])
def test_a_long_wait_keeps_the_rules_on_who_may_approve(clock, monkeypatch, who, why):
    state = waiting_state()
    sn = FakeServiceNow([REQUESTED, APPROVED], approvers=[who])
    cancelled = []
    monkeypatch.setattr(change, "cancel", lambda sn, state, notes: cancelled.append(notes))
    with pytest.raises(SystemExit):
        change.wait(sn, state, change.approval_limit(state), 15)
    assert why in cancelled[0] and "long_wait" not in change.run_state.load()


def test_a_long_wait_still_stops_on_a_rejection(clock, monkeypatch):
    state = waiting_state()
    sn = FakeServiceNow([REQUESTED, {"approval": "rejected", "state": "-4"}], approvers=[])
    cancelled = []
    monkeypatch.setattr(change, "cancel", lambda sn, state, notes: cancelled.append(notes))
    monkeypatch.setattr("notify.post", lambda state: None)
    with pytest.raises(SystemExit):
        change.wait(sn, state, change.approval_limit(state), 15)
    assert cancelled == ["Rejected in ServiceNow. The release stopped before production."]


# Every other version.

@pytest.mark.parametrize("trouble", [ASLEEP, urllib.error.URLError("timed out"), ValueError("an HTML page")])
@pytest.mark.parametrize("version", [ANY_OTHER, "1.4.0"])
def test_any_other_version_stops_on_trouble_as_it_always_has(clock, version, trouble):
    state = waiting_state(version)
    sn = FakeServiceNow([REQUESTED, trouble])
    with pytest.raises(type(trouble)):
        change.wait(sn, state, change.approval_limit(state), 15)
    assert clock.slept == [15]


@pytest.mark.parametrize("version", [ANY_OTHER, "1.4.0"])
def test_any_other_version_gives_up_after_thirty_minutes(clock, monkeypatch, version):
    state = waiting_state(version)
    sn = FakeServiceNow([REQUESTED])
    cancelled = []
    monkeypatch.setattr(change, "cancel", lambda sn, state, notes: cancelled.append(notes))
    with pytest.raises(SystemExit):
        change.wait(sn, state, change.approval_limit(state), 15)
    assert cancelled == ["No approval within 30 minutes. The release stopped before production."]
    assert len(clock.slept) == 120


@pytest.mark.parametrize("version", [ANY_OTHER, "1.4.0"])
def test_any_other_version_sets_no_flag_when_approved(clock, version):
    state = waiting_state(version)
    change.wait(FakeServiceNow([APPROVED]), state, 1800, 15)
    assert state["change"]["approved_by"] == "Alex" and "long_wait" not in state


# The planned window on the change.

class OpeningServiceNow:
    url = "https://servicenow.example"

    def __init__(self):
        self.fields = None

    def find(self, table, query, fields=None):
        return {"sys_id": f"{table}-id"}

    def call(self, method, path, body=None, **query):
        if method == "POST" and path.startswith("/api/sn_chg_rest/change/"):
            self.fields = body
            return {"sys_id": {"value": "change-id"}, "number": {"value": "CHG0030120"}}
        return {}


@pytest.mark.parametrize("version, hours", [(WAITS, 10), (ANY_OTHER, 0.5), ("1.4.0", 0.5)])
def test_the_planned_window_is_as_long_as_the_wait(monkeypatch, version, hours):
    monkeypatch.setattr(change, "requester_user", lambda sn, slack_id: {"sys_id": "sam-id"})
    monkeypatch.setattr(change, "get_parameter", lambda ssm, name: f"https://{name.split('/')[0]}.example")
    monkeypatch.setattr(change, "attach", lambda sn, change_id, path: None)
    monkeypatch.setattr(change, "code_review", lambda state: None)
    started = 1793006020.0
    monkeypatch.setattr(change.run_state, "now", lambda: started)
    state = {"service": "orders-service", "version": version, "jira_key": "ORD-140", "option": "", "requested_by": "U0SAM000001",
             "build": {"number": "120", "url": "https://jenkins.example/job/release/120/"},
             "package": {"path": "p", "sha256": "ab" * 32}, "started_at": 1793006000.0, "slack": {"channel": "C", "ts": "1"},
             "stages": {"Build": {"status": "ok", "seconds": 12.1}}}
    change.run_state.save(state)
    sn = OpeningServiceNow()
    change.open_change(sn, None, state)
    written = datetime.datetime.strptime
    start = written(sn.fields["start_date"], "%Y-%m-%d %H:%M:%S")
    assert (written(sn.fields["end_date"], "%Y-%m-%d %H:%M:%S") - start) == datetime.timedelta(hours=hours)


# Nothing else moved.

def test_the_pipeline_calls_the_wait_as_it_always_has():
    from conftest import ROOT

    groovy = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
    assert "sh 'python3 scripts/change.py wait'" in groovy
    assert "approval_wait" not in groovy and "long_wait" not in groovy
