"""The rollback's run state, Slack message, Jira lookup and ServiceNow change,
with fakes for Slack, Jira, ServiceNow and SSM.

    .venv/bin/python -m pytest -q scripts/tests
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import change  # noqa: E402
import rollback  # noqa: E402
import run_state  # noqa: E402

SAM = "U0SAM000001"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")


def release_state():
    """What run_state.py init writes for the rollback job, before rollback.py init."""
    return {"service": "orders-service", "version": "1.4.1", "commit": "abc1234", "commit_time": 1793000000,
            "jira_key": "", "option": "", "requested_by": SAM, "slack": {"channel": "C0RELEASES", "ts": "1793.0001"},
            "build": {"number": "4", "url": "https://jenkins-demo.es.nl/job/rollback/4/"},
            "started_at": 1793000000.0, "stages": {name: {"status": "waiting"} for name in run_state.STAGES}}


def rolled_back_state():
    """After deploy.py production --previous, as it writes the run state."""
    state = rollback.init(release_state())
    state["deploys"] = {"production": {"previous_number": "9", "previous_version": "1.4.1", "rolled_back_to": "1.4.0",
                                       "rolled_back_number": "8", "url": "https://prod.example/"}}
    return rollback.moved(state)


def test_init_turns_the_run_state_into_a_rollbacks():
    state = rollback.init(release_state())
    assert state["kind"] == "rollback" and state["version"] == "" and state["jira_key"] == ""
    assert list(state["stages"]) == ["Roll back", "Check rollback", "Record rollback"]
    assert state["started_at"] == 1793000000.0  # the Slack command's time, from --requested-at
    assert rollback.render(state).startswith(f"*Rollback orders-service* · production · requested by <@{SAM}>")


def test_after_the_move_the_message_names_both_versions():
    state = rolled_back_state()
    assert (state["version"], state["rolled_back_to"]) == ("1.4.1", "1.4.0")
    state["stages"]["Roll back"] = {"status": "ok", "seconds": 2.1}
    text = rollback.render(state)
    assert text.startswith("*Rollback orders-service 1.4.1 to 1.4.0* · production")
    assert ":white_check_mark:  Roll back  _3s_" in text
    assert "<https://jenkins-demo.es.nl/job/rollback/4/|Build #4>" in text


@pytest.mark.parametrize("result, reason, back, line", [
    ("rolled-back", "", True, "Rolled back from 1.4.1 to 1.4.0 in 14s."),
    ("failed", "1.4.0 fails the production check too. Look at production now", True,
     "Rolled back from 1.4.1 to 1.4.0, but 1.4.0 fails the production check too. Look at production now."),
    ("failed", "the rollback didn't run: orders-service-production has no earlier release to roll back to.", False,
     "Stopped after 14s: the rollback didn't run: orders-service-production has no earlier release to roll back to. "
     "Production didn't change."),
])
def test_the_last_line_says_how_it_ended(monkeypatch, result, reason, back, line):
    monkeypatch.setattr(run_state, "now", lambda: 1793000014.0)
    state = rolled_back_state() if back else rollback.init(release_state())
    rollback.finish(state, result, reason)
    assert state["lines"] == [line] and state["total"] == "14s"


class FakeJira:
    def __init__(self, versions, issues):
        self.versions, self.issues, self.searches = versions, issues, []

    def version(self, name, create=True):
        assert create is False
        return self.versions.get(name)

    def call(self, method, path, body=None):
        self.searches.append(path)
        return {"issues": [{"key": key} for key in self.issues]}


def test_the_rolled_back_release_is_found_by_its_fix_version():
    jira = FakeJira({"orders-service 1.4.1": {"id": "10031"}}, ["ORD-13"])
    assert rollback.find_issue(rolled_back_state(), jira) == "ORD-13"
    path, = jira.searches
    assert path.startswith("/rest/api/3/search/jql?")
    assert "fixVersion+%3D+10031" in path and "project+%3D+ORD" in path


def test_a_version_never_released_through_the_pipeline_has_no_issue():
    jira = FakeJira({}, ["ORD-1"])
    assert rollback.find_issue(rolled_back_state(), jira) is None
    assert jira.searches == []


class FakeServiceNow:
    url = "https://servicenow.example"

    def __init__(self, template=True, current_state="-5"):
        self.template, self.current_state, self.calls = template, current_state, []

    def find(self, table, query, fields=None):
        if table == "std_change_record_producer":
            assert query == "name=Rollback of orders-service^active=true"
            return {"sys_id": "template-id"} if self.template else None
        return {"sys_id": f"{table}-id", "first_name": "Sam"}

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        if method == "POST":
            return {"sys_id": {"value": "change-id"}, "number": {"value": "CHG0030042"}}
        if method == "GET":
            return {"state": self.current_state}
        return {}


@pytest.fixture
def servicenow(monkeypatch):
    monkeypatch.setattr(change, "requester_user", lambda sn, slack_id: {"sys_id": "sam-id"} if slack_id == SAM else None)
    monkeypatch.setattr(change, "get_parameter", lambda ssm, name: {
        "jira/site-url": "https://site.atlassian.net", "grafana/stack-url": "https://stack.grafana.net"}[name])
    monkeypatch.setattr(run_state, "now", lambda: 1793000020.0)
    return FakeServiceNow()


def test_a_rollback_is_recorded_from_the_rollback_template_and_closed(servicenow):
    state = rolled_back_state()
    state["jira_key"] = "ORD-13"
    change.record_rollback(servicenow, None, state, "successful", "Rolled back in 14s.")

    (method, path, fields), *updates = servicenow.calls
    assert (method, path) == ("POST", "/api/sn_chg_rest/change/standard/template-id")
    assert fields["short_description"] == "Rollback of orders-service in production: 1.4.1 to 1.4.0"
    assert fields["cmdb_ci"] == "cmdb_ci_appl-id" and fields["requested_by"] == "sam-id"
    assert "ORD-13: https://site.atlassian.net/browse/ORD-13" in fields["justification"]
    assert "version 9 (1.4.1) to version 8 (1.4.0)" in fields["implementation_plan"]
    assert fields["start_date"] == "2026-10-26 07:33:20" and fields["end_date"] == "2026-10-26 07:33:40"

    standard = "/api/sn_chg_rest/change/standard/change-id"
    assert [(m, p, b and b.get("state")) for m, p, b in updates] == [
        ("PATCH", "/api/now/table/change_request/change-id", None),  # the work note
        ("GET", "/api/now/table/change_request/change-id", None),
        ("PATCH", standard, change.SCHEDULED),
        ("PATCH", standard, change.IMPLEMENT),
        ("PATCH", standard, change.REVIEW),
        ("PATCH", standard, change.CLOSED),
    ]
    assert "Rollback steps:" in updates[0][2]["work_notes"]
    assert updates[3][2]["work_start"] == "2026-10-26 07:33:20"  # when the rollback began, not now
    assert updates[5][2] == {"state": change.CLOSED, "close_code": "successful", "close_notes": "Rolled back in 14s."}
    assert run_state.load()["change"]["number"] == "CHG0030042"


def test_without_a_jira_issue_the_change_says_so(servicenow):
    change.record_rollback(servicenow, None, rolled_back_state(), "unsuccessful", "1.4.0 fails too.")
    fields = servicenow.calls[0][2]
    assert "no Jira issue has its fix version" in fields["justification"]
    assert servicenow.calls[-1][2]["close_code"] == "unsuccessful"


def test_a_missing_rollback_template_stops_with_the_fix(servicenow, capsys):
    servicenow.template = False
    with pytest.raises(SystemExit):
        change.record_rollback(servicenow, None, rolled_back_state(), "successful", "x")
    assert "Rollback of orders-service" in capsys.readouterr().err
    assert servicenow.calls == []


def test_implement_still_starts_the_work_now_for_releases(servicenow):
    state = {"change": {"type": "normal", "sys_id": "change-id"}}
    servicenow.current_state = change.SCHEDULED
    change.implement(servicenow, state)
    assert servicenow.calls[-1][2] == {"state": change.IMPLEMENT, "work_start": "2026-10-26 07:33:40"}
