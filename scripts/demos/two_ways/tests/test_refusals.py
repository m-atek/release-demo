"""The two refusals through the real change.py, deploy.py and notify.py: the first day's checks, as far as they go offline.
ServiceNow, JFrog, Lambda and Slack are fakes. What the fakes can't say is listed in prep/28: whether ServiceNow lets
release.pipeline cancel a change in Implement, and how each screen shows these lines."""

import hashlib

import pytest

import change
import cheat
import pipeline_calls
import run_state
from conftest import (ALEX, NOTHING_REACHED, PACKAGE_PATH, Now, run_deploy, stage_failed, stopped, swapped, waiting_state)

REFUSED_SELF = "Approved by the person who asked for the release. Refused by the pipeline: the person who asks can't approve."
PIPELINE = pipeline_calls.LIBRARY / "releasePipeline.groovy"


def test_stopped_here_reads_as_the_pipeline_s():
    text = pipeline_calls.groovy_function(PIPELINE.read_text(), "stopped")
    assert "replaceAll(/\\.+$/, '')" in text
    assert text.index("notify.py finish failed") < text.index("change.py cancel")
    assert "state('deploys.production.number')" in text
    assert 'cancel --notes "The release stopped: $REASON. Nothing changed in production."' in text


# The first refusal: Sam's approval, recorded by an admin.

def test_one_row_in_sam_s_name_is_enough_while_servicenow_still_says_requested(slack, no_waiting):
    sn = Now(approval="requested")
    cheat.approve(sn, "CHG0030120")
    state = waiting_state()
    with pytest.raises(SystemExit) as stop:
        change.wait(sn, state, timeout=60, interval=15)
    assert stop.value.code == 1
    assert sn.change["approval"] == "requested"
    assert sn.change["state"] == change.CANCELED
    assert sn.close_notes == [REFUSED_SELF]
    assert sn.work_notes == [REFUSED_SELF]


def test_it_is_refused_whatever_servicenow_says_even_beside_alex_s_approval(slack, no_waiting):
    sn = Now(approval="approved")
    next(row for row in sn.rows if row["approver"] == ALEX)["state"] = "approved"
    cheat.approve(sn, "CHG0030120")
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=15)
    assert sn.close_notes == [REFUSED_SELF]


def test_alex_s_approval_alone_passes(slack, no_waiting):
    sn = Now(approval="approved")
    next(row for row in sn.rows if row["approver"] == ALEX)["state"] = "approved"
    change.wait(sn, waiting_state(), timeout=60, interval=15)
    assert run_state.load()["change"]["approved_by"] == "Alex"
    assert sn.close_notes == [] and sn.change["state"] == "-4"
    assert "lines" not in run_state.load() and slack == []


def test_the_comparison_is_with_who_asked_in_this_run(slack, no_waiting):
    """Sam's row refuses Sam's change. Had someone else asked, Sam's approval would be an approval like any other."""
    sn = Now(approval="approved")
    cheat.approve(sn, "CHG0030120")
    state = waiting_state()
    state["change"]["requested_by"] = ALEX
    run_state.save(state)
    change.wait(sn, state, timeout=60, interval=15)
    assert run_state.load()["change"]["approved_by"] == "Sam"


def test_a_change_with_no_approval_yet_keeps_waiting(slack, monkeypatch):
    """So the pipeline looks every 15 seconds until the row is there: the refusal comes within one look of it."""
    sn = Now()
    looks = []

    def next_look(seconds):
        looks.append(seconds)
        cheat.approve(sn, "CHG0030120")

    monkeypatch.setattr(change.time, "sleep", next_look)
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=15)
    assert looks == [15] and sn.close_notes == [REFUSED_SELF]


def test_after_the_refusal_slack_ends_on_the_stop_line_and_the_change_keeps_its_first_notes(slack, no_waiting):
    sn = Now()
    cheat.approve(sn, "CHG0030120")
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=15)
    stage_failed("Approval", 64.2)
    stopped(sn)
    assert sn.close_notes == [REFUSED_SELF]
    assert sn.work_notes[-1] == "The release stopped: script returned exit code 1. Nothing changed in production."
    message = slack[-1]
    assert message.endswith(f"Stopped after 3m 35s. {NOTHING_REACHED}")
    assert ":x:  Approval  _1m 05s_" in message
    assert "○  Production" in message and "○  Check production" in message and "○  Record" in message


# The second refusal: the stored package overwritten while the change waits.

def test_with_the_package_as_it_was_production_gets_those_exact_bytes(stack, monkeypatch, capsys, slack):
    package, _, functions = stack
    waiting_state(package)
    run_deploy(monkeypatch, "production")
    assert functions.zips == [package]
    assert [call[0] for call in functions.writes()] == ["update_function_code", "publish_version", "update_alias"]
    assert f"SHA-256 verified: {hashlib.sha256(package).hexdigest()}" in capsys.readouterr().out
    state = run_state.load()
    assert state["deploys"]["production"]["number"] == "44"
    assert "lines" not in state and "stop_reason" not in state


def test_a_swapped_package_stops_the_deploy_before_any_call_to_lambda(stack, monkeypatch, capsys, slack):
    package, store, functions = stack
    waiting_state(package)
    swapped(store, monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        run_deploy(monkeypatch, "production")
    assert stop.value.code == 1
    assert functions.calls == [] and functions.zips == []
    actual, recorded = hashlib.sha256(store.files[PACKAGE_PATH]).hexdigest(), hashlib.sha256(package).hexdigest()
    assert capsys.readouterr().err.strip() == (f"{PACKAGE_PATH} has SHA-256 {actual}, but the build recorded {recorded}. "
                                               "Not deploying a package that changed after it was published.")
    assert "production" not in run_state.load()["deploys"]


def test_the_stop_says_nothing_reached_production_and_the_change_is_cancelled_from_implement(stack, monkeypatch, capsys, slack):
    package, store, _ = stack
    waiting_state(package, lines=["Approved by Alex in ServiceNow."])
    swapped(store, monkeypatch)
    sn = Now(approval="approved", state=change.IMPLEMENT)
    with pytest.raises(SystemExit):
        run_deploy(monkeypatch, "production")
    stage_failed("Production", 2.4)
    stopped(sn)
    assert sn.change["state"] == change.CANCELED
    assert sn.close_notes[0].startswith("The release stopped: ") and sn.close_notes[0].endswith(" Nothing changed in production.")
    assert slack[-1].endswith(f"Stopped after 3m 35s. {NOTHING_REACHED}")
    assert "after the production deploy" not in slack[-1]
    assert ":x:  Production  _3s_" in slack[-1]
    assert "○  Check production" in slack[-1] and "○  Record" in slack[-1]


def test_a_refused_cancel_from_implement_doesn_t_change_what_slack_says(stack, monkeypatch, capsys, slack):
    """The first-day check this can't answer: if ServiceNow refuses, the pipeline's sh(returnStatus: true) ignores it,
    Slack's stop line stands, and the change stays in Implement until the fix in prep/28."""
    package, store, _ = stack
    waiting_state(package)
    swapped(store, monkeypatch)
    sn = Now(approval="approved", state=change.IMPLEMENT, cancel_from_implement=False)
    with pytest.raises(SystemExit):
        run_deploy(monkeypatch, "production")
    stage_failed("Production", 2.4)
    with pytest.raises(SystemExit):
        stopped(sn)
    assert sn.change["state"] == change.IMPLEMENT and sn.close_notes == []
    assert slack[-1].endswith(NOTHING_REACHED)
    text = pipeline_calls.groovy_function(PIPELINE.read_text(), "stopped")
    assert "sh(returnStatus: true, script: 'python3 scripts/change.py cancel" in text


def test_the_change_is_in_implement_before_the_deploy_is_tried():
    """Why the cancel above starts from Implement: the Production stage's two steps, in this order."""
    text = PIPELINE.read_text()
    production = text[text.index("releaseStage('Production'"):text.index("releaseStage('Check production'")]
    assert production.index("change.py implement") < production.index("${deploy} ${service} production")


def test_the_same_check_stops_a_swap_before_staging_with_no_change_to_cancel(stack, monkeypatch, capsys, slack):
    package, store, functions = stack
    state = waiting_state(package)
    del state["change"]
    state["deploys"] = {}
    run_state.save(state)
    swapped(store, monkeypatch)
    with pytest.raises(SystemExit):
        run_deploy(monkeypatch, "staging")
    assert functions.calls == []
