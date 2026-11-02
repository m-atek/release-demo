"""The clock: it opens once, stops only on a clean grype.json, and stops at the last holder.

    .venv/bin/python -m pytest -q scripts/demos/patch_clock/tests
"""

import json

import pytest

import clock
import run_state
from conftest import SAM, saved, saved_bytes

STARTED = 1793000000.0
CVE = "CVE-2026-111497"
IDS = [CVE, "GHSA-9V8H-QQGM-4M35"]
BROTLI = {"package": "brotli", "version": "1.2.0"}


@pytest.fixture
def folders(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "FOLDER", tmp_path / "clocks")
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(run_state, "now", lambda: STARTED + 520)
    return reports


def state(service="orders-service", version="1.12.2"):
    return {"service": service, "version": version, "change": {"number": "CHG0030090", "sys_id": "abc"}}


def clean_scan(reports):
    (reports / "grype.json").write_text(json.dumps(saved("grype-scan-brotli-1.2.1.json")))
    (reports / "sbom.cdx.json").write_bytes(saved_bytes("sbom-brotli-1.2.1.cdx.json"))


def dirty_scan(reports):
    (reports / "grype.json").write_text(json.dumps(saved("grype-scan-brotli-1.2.0.json")))
    (reports / "sbom.cdx.json").write_bytes(saved_bytes("sbom-brotli-1.2.0.cdx.json"))


def stopping(reports, **kwargs):
    lines, notes = [], []
    ended = clock.stop(kwargs.pop("state", state()), reports, note=lambda s, text: notes.append(text), line=lambda s, text: lines.append(text))
    return ended, lines, notes


def test_the_first_search_opens_it_and_a_later_one_keeps_the_first_time(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    again = clock.start(CVE, STARTED + 60, {"orders-service": BROTLI}, IDS)
    assert again["requested_at"] == STARTED
    (opened,) = clock.open_clocks()
    assert opened == {"id": CVE, "requested_at": STARTED, "ids": sorted(IDS), "holders": {"orders-service": BROTLI}}


def test_a_later_search_adds_a_new_holder_and_keeps_the_first_time(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    opened = clock.start(CVE, STARTED + 60, {"quote-service": BROTLI}, IDS)
    assert opened["requested_at"] == STARTED and set(opened["holders"]) == {"orders-service", "quote-service"}


def test_a_clean_release_of_the_last_holder_stops_it_with_the_line_and_the_note(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    clean_scan(folders)
    ended, lines, notes = stopping(folders)
    assert ended == [CVE] and clock.open_clocks() == []
    assert lines == ["CHG0030090 closed. Search to closed change: 8m 40s."]
    assert notes == ["CVE-2026-111497: orders-service 1.12.2 holds brotli 1.2.1. Search to closed change: 8m 40s."]


def test_the_time_is_never_rounded_down(folders, monkeypatch):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    clean_scan(folders)
    monkeypatch.setattr(run_state, "now", lambda: STARTED + 519.2)
    _, lines, _ = stopping(folders)
    assert lines == ["CHG0030090 closed. Search to closed change: 8m 40s."]


def test_it_stops_only_on_a_clean_scan(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    dirty_scan(folders)
    ended, lines, notes = stopping(folders)
    assert ended == [] and lines == [] and notes == []
    assert clock.open_clocks()[0]["holders"] == {"orders-service": BROTLI}


@pytest.mark.parametrize("report", [None, "not json", "{}"])
def test_no_report_or_an_unreadable_one_is_not_clean(folders, report):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    if report is not None:
        (folders / "grype.json").write_text(report)
    assert stopping(folders)[0] == [] and len(clock.open_clocks()) == 1


def test_a_finding_of_another_id_is_still_clean_of_this_one(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    other = saved("grype-scan-brotli-1.2.0.json")
    other["matches"][0]["vulnerability"]["id"] = "GHSA-aaaa-bbbb-cccc"
    other["matches"][0]["relatedVulnerabilities"] = [{"id": "CVE-2020-1"}]
    (folders / "grype.json").write_text(json.dumps(other))
    assert stopping(folders)[0] == [CVE]


def test_a_match_through_its_ghsa_is_not_clean(folders):
    clock.start("GHSA-9v8h-qqgm-4m35", STARTED, {"orders-service": BROTLI}, ["GHSA-9v8h-qqgm-4m35"])
    dirty_scan(folders)  # the match's id is the GHSA, in any case
    assert stopping(folders)[0] == []


def test_another_services_release_changes_nothing(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    clean_scan(folders)
    assert stopping(folders, state=state("pricing-api", "2.1.2"))[0] == []
    assert len(clock.open_clocks()) == 1


def test_it_stops_at_the_last_holder_not_the_first(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI, "quote-service": BROTLI}, IDS)
    clean_scan(folders)
    ended, lines, notes = stopping(folders)
    assert ended == [] and lines == [] and notes == []
    assert set(clock.open_clocks()[0]["holders"]) == {"quote-service"}
    ended, lines, notes = stopping(folders, state=state("quote-service", "1.3.0"))
    assert ended == [CVE] and lines == ["CHG0030090 closed. Search to closed change: 8m 40s."]
    assert notes == ["CVE-2026-111497: quote-service 1.3.0 holds brotli 1.2.1. Search to closed change: 8m 40s."]


def test_a_release_that_drops_the_package_says_so(folders):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    (folders / "grype.json").write_text(json.dumps(saved("grype-scan-brotli-1.2.1.json")))
    (folders / "sbom.cdx.json").write_bytes(saved_bytes("sbom-empty.cdx.json"))
    assert stopping(folders)[2] == ["CVE-2026-111497: orders-service 1.12.2 no longer holds brotli. Search to closed change: 8m 40s."]


def test_a_message_that_fails_never_fails_the_release(folders, capsys):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    clean_scan(folders)

    def asleep(state_, text):
        raise SystemExit("ServiceNow is asleep")

    lines = []
    assert clock.stop(state(), folders, note=asleep, line=lambda s, text: lines.append(text)) == [CVE]
    assert lines and "not sent (SystemExit)" in capsys.readouterr().out and clock.open_clocks() == []


def test_with_the_switch_off_a_release_does_nothing(folders, monkeypatch):
    clock.start(CVE, STARTED, {"orders-service": BROTLI}, IDS)
    monkeypatch.setattr(clock.SWITCH, "is_on", lambda ssm=None: False)
    monkeypatch.setattr(clock.sys, "argv", ["clock.py", "stop"])
    monkeypatch.setattr(clock, "stop", lambda *a, **k: pytest.fail("the switch is off"))
    assert clock.main() == 0


def test_the_slack_line_lands_in_the_status_message(folders, tmp_path, monkeypatch):
    import notify

    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")
    posted = []
    monkeypatch.setattr(notify, "post", posted.append)
    release = {**state(), "stages": {}, "jira_key": "ORD-103", "build": {}, "requested_by": SAM, "slack": {"channel": "C", "ts": "1"},
               "lines": ["Live in 3m 51s."]}
    clock.slack_line(release, "CHG0030090 closed. Search to closed change: 8m 40s.")
    assert posted[0]["lines"] == ["Live in 3m 51s.", "CHG0030090 closed. Search to closed change: 8m 40s."]
    assert notify.render(posted[0]).endswith("Live in 3m 51s.\n\nCHG0030090 closed. Search to closed change: 8m 40s.")
