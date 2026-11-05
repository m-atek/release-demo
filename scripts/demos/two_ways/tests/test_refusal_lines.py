"""What the small shared change adds: Slack and the change say why. Only a refusal reaches these lines, and no other
video refuses this way, so a release that passes writes nothing new (test_refusals holds that)."""

import hashlib

import pytest

import change
import cheat
import pipeline_calls
import run_state
from conftest import HEX, NOTHING_REACHED, PACKAGE_PATH, Now, run_deploy, stage_failed, stopped, swapped, waiting_state

PIPELINE = pipeline_calls.LIBRARY / "releasePipeline.groovy"


def test_jenkins_reads_the_reason_from_the_run_state_as_the_other_cis_do():
    text = pipeline_calls.groovy_function(PIPELINE.read_text(), "stopped")
    assert "state('stop_reason') ?: error.message" in text
    for other in (".gitlab-ci.yml", "azure-pipelines.yml", ".github/workflows/release.yml"):
        assert "reason=$(state stop_reason)" in (pipeline_calls.ROOT / other).read_text()


def test_slack_says_why_in_two_short_lines_above_the_stop_line(slack, no_waiting):
    sn = Now()
    cheat.approve(sn, "CHG0030120")
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=15)
    stage_failed("Approval", 64.2)
    stopped(sn)
    assert slack[-1].split("\n\n")[-3:] == ["Approved by the person who asked for the release.",
                                             "Refused by the pipeline: the person who asks can't approve.",
                                             f"Stopped after 3m 35s. {NOTHING_REACHED}"]


@pytest.mark.parametrize("row, what, rule", [
    ("pipeline-id", "Approved by a machine user.", "Refused by the pipeline: only a person approves a change."),
    ("notes-id", "Approved by a machine user.", "Refused by the pipeline: only a person approves a change."),
])
def test_a_machine_user_s_close_notes_read_as_they_did_and_slack_gets_them_too(row, what, rule, slack, no_waiting):
    sn = Now(approval="approved")
    sn.rows.append({"sys_id": "row-1", "sysapproval": "change-id", "approver": row, "state": "approved"})
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=15)
    assert sn.close_notes == [f"{what} {rule}"]
    assert run_state.load()["lines"] == [what, rule]


def test_the_change_names_both_checksums_and_slack_the_first_eight_of_each(stack, monkeypatch, capsys, slack):
    package, store, _ = stack
    waiting_state(package, lines=["Approved by Alex in ServiceNow."])
    swapped(store, monkeypatch)
    sn = Now(approval="approved", state=change.IMPLEMENT)
    with pytest.raises(SystemExit):
        run_deploy(monkeypatch, "production")
    stage_failed("Production", 2.4)
    stopped(sn)
    actual, recorded = hashlib.sha256(store.files[PACKAGE_PATH]).hexdigest(), hashlib.sha256(package).hexdigest()
    assert sn.close_notes == ["The release stopped: Not deploying a package that changed after it was published. "
                              f"Stored SHA-256 {actual}, the build recorded {recorded}. Nothing changed in production."]
    assert slack[-1].split("\n\n")[-4:] == ["Approved by Alex in ServiceNow.",
                                             "Not deploying a package that changed after it was published.",
                                             f"Stored SHA-256 {actual[:8]}. The build recorded {recorded[:8]}.",
                                             f"Stopped after 3m 35s. {NOTHING_REACHED}"]


def test_slack_s_lines_stay_short_for_a_4_5_crop_and_hold_no_whole_checksum(stack, monkeypatch, capsys, slack):
    package, store, _ = stack
    waiting_state(package)
    swapped(store, monkeypatch)
    with pytest.raises(SystemExit):
        run_deploy(monkeypatch, "production")
    lines = run_state.load()["lines"]
    assert [len(line) for line in lines] == [60, 53]
    assert not any(HEX.search(line) for line in lines)


def test_the_log_s_line_reads_as_it_always_did(stack, monkeypatch, capsys, slack):
    package, store, _ = stack
    waiting_state(package)
    swapped(store, monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run_deploy(monkeypatch, "production")
    actual, recorded = hashlib.sha256(store.files[PACKAGE_PATH]).hexdigest(), hashlib.sha256(package).hexdigest()
    assert capsys.readouterr().err.strip() == (f"{PACKAGE_PATH} has SHA-256 {actual}, but the build recorded {recorded}. "
                                               "Not deploying a package that changed after it was published.")
