"""Video 31's mock (REVIEW decision 161): services.yaml's mock_waited_hours adds six hours to an approved wait,
in Slack's Approval row, "Live in", the change's work note and the event the dashboard reads, for 1.33.0 only."""

import sys

import pytest

import change
import events
import notify
import run_state
from conftest import ANY_OTHER, WAITS

SIX_HOURS = 6 * 3600


def state_for(version, service="orders-service", approval=None):
    stages = {"Approval": approval or {"status": "running"}, "Production": {"status": "waiting"}}
    return {"service": service, "version": version, "started_at": 1793000000.0, "stages": stages, "jira_key": "ORD-111",
            "build": {"number": "124"}, "slack": {"channel": "C", "ts": "1"}}


def test_services_yaml_mocks_six_hours_for_the_named_version_only():
    assert run_state.mock_wait(state_for(WAITS)) == SIX_HOURS
    assert run_state.mock_wait(state_for(ANY_OTHER)) == 0
    assert run_state.mock_wait(state_for("1.4.0")) == 0
    assert run_state.mock_wait(state_for(WAITS, service="catalog")) == 0
    assert run_state.mock_wait({}) == 0


@pytest.fixture
def notify_run(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "post", lambda state: posted.append(state))

    def run(state, *argv, elapsed=300.0):
        run_state.save(state)
        monkeypatch.setattr(run_state, "now", lambda: 1793000000.0 + elapsed)
        monkeypatch.setattr(sys, "argv", ["notify.py", *argv])
        notify.main()
        return run_state.load()

    return run


def test_the_approval_row_gets_six_hours_when_approved(notify_run):
    saved = notify_run(state_for(WAITS), "stage", "Approval", "ok", "--seconds", "301.5")
    assert saved["stages"]["Approval"]["seconds"] == pytest.approx(SIX_HOURS + 301.5)


@pytest.mark.parametrize("name, status", [("Approval", "failed"), ("Production", "ok"), ("Change", "ok")])
def test_no_other_row_and_no_failed_approval_is_touched(notify_run, name, status):
    state = state_for(WAITS)
    state["stages"][name] = {"status": "running"}
    saved = notify_run(state, "stage", name, status, "--seconds", "12.0")
    assert saved["stages"][name]["seconds"] == pytest.approx(12.0)


def test_other_versions_keep_their_real_approval_time(notify_run):
    saved = notify_run(state_for(ANY_OTHER), "stage", "Approval", "ok", "--seconds", "301.5")
    assert saved["stages"]["Approval"]["seconds"] == pytest.approx(301.5)


def test_live_in_and_the_split_agree_with_the_mocked_row(notify_run):
    state = state_for(WAITS, approval={"status": "ok", "seconds": SIX_HOURS + 300.0})
    state["long_wait"] = True
    saved = notify_run(state, "finish", "live", elapsed=470.0)
    assert saved["total"] == run_state.duration(SIX_HOURS + 470.0)
    assert "Waited for approval: 6h 05m 00s." in saved["close_notes_extra"]
    assert "Everything else: 2m 50s." in saved["close_notes_extra"]


def test_a_stop_before_approval_keeps_the_real_total(notify_run):
    saved = notify_run(state_for(WAITS), "finish", "failed", elapsed=470.0)
    assert saved["total"] == run_state.duration(470.0)


def test_the_change_work_note_carries_the_same_seconds():
    assert change.mocked_note(state_for(WAITS), "Approval: ok in 301.5s") == f"Approval: ok in {SIX_HOURS + 301.5:.1f}s"
    assert change.mocked_note(state_for(ANY_OTHER), "Approval: ok in 301.5s") == "Approval: ok in 301.5s"
    assert change.mocked_note(state_for(WAITS), "Approval: failed in 301.5s") == "Approval: failed in 301.5s"
    assert change.mocked_note(state_for(WAITS), "Production: ok in 13.0s") == "Production: ok in 13.0s"


def test_the_event_the_dashboard_reads_gets_the_same_seconds(monkeypatch):
    run_state.save(state_for(WAITS))
    sent = []
    monkeypatch.setattr(events, "ssm_client", lambda: None)
    monkeypatch.setattr(events, "to_splunk", lambda ssm, body: sent.append(body) or 200)
    monkeypatch.setattr(events, "to_loki", lambda ssm, body: 204)
    monkeypatch.setattr(sys, "argv", ["events.py", "--stage", "Approval", "--result", "ok", "--seconds", "301.5"])
    events.main()
    assert sent[0]["duration_seconds"] == pytest.approx(SIX_HOURS + 301.5)
