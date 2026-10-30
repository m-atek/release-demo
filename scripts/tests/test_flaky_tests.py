"""Per-test results, the rerun in place and the quarantine (demo 15): tests_run.py,
junit_results.py, quarantine.py, events.py tests, flaky.py and change.py's
attachments. Suites run for real, in temporary folders; Grafana, Jira, GitHub,
Slack and ServiceNow are fakes.

    .venv/bin/python -m pytest -q scripts/tests/test_flaky_tests.py
"""

import datetime
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import change  # noqa: E402
import events  # noqa: E402
import flaky  # noqa: E402
import junit_results  # noqa: E402
import quarantine  # noqa: E402
import run_state  # noqa: E402
import tests_run  # noqa: E402
from common import ROOT  # noqa: E402

TODAY = datetime.date(2026, 11, 2)

# A suite whose tests behave as the names say. test_sometimes fails on its first
# run in a folder and passes after, as a timing test does now and then.
SUITE = '''
import pathlib


def test_fine():
    assert True


def test_sometimes():
    marker = pathlib.Path(__file__).with_name("ran-once")
    if not marker.exists():
        marker.write_text("1")
        assert False, "too slow this time"


def test_broken():
    assert {broken}
'''


def suite(tmp_path, broken=False):
    directory = tmp_path / "service"
    (directory / "tests").mkdir(parents=True)
    (directory / "tests" / "test_suite.py").write_text(SUITE.format(broken=not broken))
    return directory


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """tests_run.py against a service folder of our own, its reports in tmp_path."""
    reports = tmp_path / "reports"
    qfile = tmp_path / "quarantine.yaml"
    monkeypatch.setattr(tests_run, "REPORTS", reports)
    monkeypatch.setattr(tests_run, "ROOT", tmp_path)
    monkeypatch.setattr(quarantine, "today", lambda: TODAY)
    monkeypatch.setattr(run_state, "REPORTS", reports)
    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")

    def run(directory, entries=(), rerun=1):
        qfile.write_text(quarantine.render(list(entries)))
        config = {"name": "orders-service", "dir": directory,
                  "tests": {"rerun_failed": rerun, "quarantine": {"file": "quarantine.yaml"}}}
        monkeypatch.setattr(tests_run, "service", lambda name: config)
        monkeypatch.setattr(sys, "argv", ["tests_run.py", "orders-service"])
        code = tests_run.main()
        return code, junit_results.read(reports)

    run.reports = reports
    return run


def outcomes(results):
    return {junit_results.short(t["test"]): (t["outcome"], t["attempts"]) for t in results["tests"]}


def entry(test, until, issue="ORD-95"):
    return {"service": "orders-service", "test": f"tests/test_suite.py::{test}", "issue": issue,
            "since": TODAY - datetime.timedelta(days=3), "until": until, "reason": "5 of 60 runs in 14 days failed, then passed on rerun."}


def test_a_flaky_test_is_rerun_once_in_place_and_the_stage_passes(tmp_path, runner, capfd):
    code, results = runner(suite(tmp_path))
    assert code == 0
    assert outcomes(results) == {"test_fine": ("passed", 1), "test_sometimes": ("flaky", 2), "test_broken": ("passed", 1)}
    assert junit_results.lines(results) == ["Flaky: test_sometimes failed, then passed on rerun."]
    log = capfd.readouterr().out
    assert "FAILED tests/test_suite.py::test_sometimes" in log
    assert "tests/test_suite.py::test_sometimes PASSED" in log
    # junit.xml keeps the final result, so Jenkins' junit step sees no failure.
    assert 'failures="0"' in (runner.reports / "junit.xml").read_text()
    attempts = (runner.reports / "junit-attempts.xml").read_text()
    assert attempts.count('name="test_sometimes"') == 2 and "too slow this time" in attempts


def test_a_test_that_fails_twice_fails_the_stage(tmp_path, runner):
    code, results = runner(suite(tmp_path, broken=True))
    assert code == 1
    assert outcomes(results)["test_broken"] == ("failed", 2)
    assert junit_results.lines(results) == ["Flaky: test_sometimes failed, then passed on rerun.",
                                            "Failed twice: test_broken."]


def test_a_clean_suite_behaves_as_before(tmp_path, runner):
    directory = suite(tmp_path)
    (directory / "tests" / "ran-once").write_text("1")
    code, results = runner(directory)
    assert code == 0
    assert {o for o, _ in outcomes(results).values()} == {"passed"}
    assert not (runner.reports / "junit-attempts.xml").exists()
    assert (runner.reports / "coverage.xml").exists()
    assert junit_results.lines(results) == []


def test_a_quarantined_test_runs_but_cant_fail_the_stage_and_isnt_rerun(tmp_path, runner):
    code, results = runner(suite(tmp_path, broken=True), [entry("test_broken", TODAY + datetime.timedelta(days=5)),
                                                          entry("test_fine", TODAY)])
    assert code == 0
    found = outcomes(results)
    assert found["test_broken"] == ("quarantined-failed", 1)
    assert found["test_fine"] == ("quarantined-passed", 1)  # until today still applies
    assert found["test_sometimes"] == ("flaky", 2)
    quarantined = next(t for t in results["tests"] if t["test"].endswith("test_broken"))
    assert quarantined["quarantine"] == "ORD-95" and quarantined["quarantine_until"] == "2026-11-07"


def test_an_ended_quarantine_fails_the_stage_at_its_start(tmp_path, runner):
    directory = suite(tmp_path)
    code, results = runner(directory, [entry("test_sometimes", TODAY - datetime.timedelta(days=1))])
    assert code == 1
    assert results["tests"] == [] and not (directory / "tests" / "ran-once").exists()  # nothing ran
    ended = json.loads((runner.reports / "quarantine.json").read_text())["ended"]
    assert ended == ["Quarantine ended on 1 Nov 2026 for test_sometimes (ORD-95). Fix it or extend it."]


def test_an_entry_for_a_test_the_version_doesnt_have_is_ignored(tmp_path, runner):
    code, results = runner(suite(tmp_path), [entry("test_gone", TODAY - datetime.timedelta(days=30))])
    assert code == 0 and results["tests"]


def test_without_a_rerun_setting_a_failure_fails_at_once(tmp_path, runner):
    code, results = runner(suite(tmp_path), rerun=0)
    assert code == 1
    assert outcomes(results)["test_sometimes"] == ("failed", 1)
    assert junit_results.lines(results) == []


def test_the_quarantine_file_reads_back_what_it_writes():
    entries = [entry("test_sometimes", datetime.date(2026, 11, 16))]
    assert quarantine.load_text(quarantine.render(entries)) == entries
    assert quarantine.load_text(quarantine.render([])) == []
    assert quarantine.render([]).startswith("# Tests whose failures don't stop a release")
    shipped = ROOT / "tests" / "quarantine.yaml"
    assert quarantine.load(shipped) == [] and shipped.read_text() == quarantine.render([])


def results_of(outcome_attempts, first=40.0, rerun=0.0):
    return {"tests": [{"test": f"tests/test_x.py::{name}", "outcome": outcome, "attempts": attempts, "seconds": 1.0,
                       "quarantine": None, "quarantine_until": None}
                      for name, (outcome, attempts) in outcome_attempts.items()],
            "first_pass_seconds": first, "rerun_seconds": rerun, "reran": rerun > 0}


def test_reruns_avoided_and_seconds_saved_equal_a_hand_calculation():
    """Acceptance check 8: three runs, by hand. A flaky run saves its Build and first
    pass less its rerun; a clean run saves nothing; a run that still failed avoided nothing."""
    runs = [
        (results_of({"test_a": ("flaky", 2), "test_b": ("passed", 1)}, first=38.4, rerun=6.1), 21.5),
        (results_of({"test_a": ("passed", 1), "test_b": ("passed", 1)}, first=37.9), 20.8),
        (results_of({"test_a": ("flaky", 2), "test_b": ("failed", 2)}, first=39.0, rerun=7.0), 22.0),
    ]
    summaries = [junit_results.summary(results, build) for results, build in runs]
    assert [s["avoided"] for s in summaries] == [1, 0, 0]
    assert [s["saved_seconds"] for s in summaries] == [round(21.5 + 38.4 - 6.1, 1), 0.0, 0.0]
    assert sum(s["saved_seconds"] for s in summaries) / 60 == pytest.approx(53.8 / 60)
    assert summaries[2]["failed_twice"] == 1 and summaries[2]["flaky"] == 1


def test_events_name_each_test_and_carry_the_run(tmp_path, monkeypatch):
    state = {"service": "orders-service", "version": "1.5.0", "commit": "abc1234",
             "build": {"number": "41", "url": "https://jenkins-demo.es.nl/job/orders-service-tests/41/"},
             "stages": {"Build": {"status": "ok", "seconds": "21.5"}}}
    results = results_of({"test_delivery_estimate_timeout": ("flaky", 2), "test_page": ("passed", 1)}, 38.4, 6.1)
    results["tests"][1].update(quarantine="ORD-95", quarantine_until="2026-11-16", outcome="quarantined-passed")
    sent = events.test_events(state, "tests", results)
    first, second, summary = sent
    assert first == {"event": "test", "service": "orders-service", "version": "1.5.0", "job": "tests", "build": "41",
                     "build_url": state["build"]["url"], "commit": "abc1234",
                     "test": "tests/test_x.py::test_delivery_estimate_timeout", "name": "test_delivery_estimate_timeout",
                     "outcome": "flaky", "attempts": 2, "seconds": 1.0, "flaky": 1, "failed_twice": 0}
    assert second["quarantine"] == "ORD-95" and second["quarantine_until_ms"] > 0
    assert summary["event"] == "summary" and summary["saved_seconds"] == 53.8 and summary["build_seconds"] == 21.5
    # No field the Releases dashboard's release table reads, so test events never look like a release.
    assert not {"stage", "result"} & set().union(*sent)


def test_test_events_go_out_as_one_stream_with_three_labels(monkeypatch):
    posted = []
    monkeypatch.setattr(events, "get_parameter", lambda ssm, name: {
        "splunk/hec-token": "t", "grafana/logs-url": "https://logs.example", "grafana/logs-user": "1",
        "grafana/logs-token": "x"}[name])
    monkeypatch.setattr(events, "send", lambda url, body, headers: posted.append(body) or 204)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    splunk = []
    monkeypatch.setattr(events.urllib.request, "urlopen", lambda request, timeout: splunk.append(request.data) or Response())
    sent = [{"event": "test", "service": "orders-service", "test": "a"}, {"event": "summary", "service": "orders-service"}]
    assert events.send_tests(None, sent) == (200, 204)
    (stream,) = posted[0]["streams"]
    assert stream["stream"] == {"source": "release-demo", "service": "orders-service", "kind": "test"}
    assert [json.loads(line) for _, line in stream["values"]] == sent
    assert splunk[0].count(b'"sourcetype": "release:test"') == 2


GRAFANA_REPLY = {"results": {"A": {"status": 200, "frames": [
    {"schema": {"fields": [{"name": "Time"}, {"name": "Value", "labels": {
        "test": "tests/test_handler.py::test_delivery_estimate_timeout", "outcome": "flaky"}}]},
     "data": {"values": [[1793000000000], [8]]}},
    {"schema": {"fields": [{"name": "Time"}, {"name": "Value", "labels": {
        "test": "tests/test_handler.py::test_delivery_estimate_timeout", "outcome": "passed"}}]},
     "data": {"values": [[1793000000000], [88]]}},
    {"schema": {"fields": [{"name": "Time"}, {"name": "Value", "labels": {
        "test": "tests/test_handler.py::test_page_shows_delivery_estimate", "outcome": "passed"}}]},
     "data": {"values": [[1793000000000], [96]]}},
]}}}


def test_decide_reads_grafanas_counts_and_picks_a_test_flaky_often_enough():
    counts = flaky.parse_counts(GRAFANA_REPLY)
    timeout = "tests/test_handler.py::test_delivery_estimate_timeout"
    assert counts[timeout] == {"flaky": 8, "passed": 88}
    tests = flaky.version_tests(ROOT / "demo-states" / "orders-service" / "1.5.0")
    assert timeout in tests and "tests/test_handler.py::test_delivery_estimate_per_region" in tests
    assert flaky.to_quarantine(tests, counts, [], "orders-service", 3) == [(timeout, 8, 96)]
    assert flaky.to_quarantine(tests, counts, [], "orders-service", 9) == []
    have = [entry("x", TODAY) | {"test": timeout}]
    assert flaky.to_quarantine(tests, counts, have, "orders-service", 3) == []
    # A version without the test, such as the main video's, has nothing to quarantine.
    old = flaky.version_tests(ROOT / "demo-states" / "orders-service" / "1.4.0")
    assert flaky.to_quarantine(old, counts, [], "orders-service", 3) == []


def test_decide_opens_the_issue_commits_the_entry_and_keeps_the_line_for_later(tmp_path, monkeypatch):
    state = {"service": "orders-service", "version": "1.5.0", "build": {"number": "77", "url": "https://jenkins/77/"}}
    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")
    run_state.save(state)
    monkeypatch.setattr(flaky, "ROOT", tmp_path)
    config = flaky.service("orders-service")
    config["dir"] = ROOT / "demo-states" / "orders-service" / "1.5.0"  # as stage.py puts it in the workspace
    monkeypatch.setattr(flaky, "service", lambda name: config)
    monkeypatch.setattr(flaky, "ssm_client", lambda: None)
    monkeypatch.setattr(flaky, "get_parameter", lambda ssm, name: {"grafana/stack-url": "https://stack.grafana.net",
                                                                   "grafana/service-account-token": "glsa"}[name])
    monkeypatch.setattr(flaky, "grafana_counts", lambda *args: flaky.parse_counts(GRAFANA_REPLY))
    monkeypatch.setattr(quarantine, "today", lambda: TODAY)
    created, commits = [], []

    class FakeJira:
        def create(self, kind, summary, description, labels=(), due=None):
            created.append((kind, summary, labels, due, description))
            return "ORD-95"

    monkeypatch.setitem(sys.modules, "jira", type(sys)("jira"))
    sys.modules["jira"].Jira = FakeJira
    monkeypatch.setattr(flaky, "commit_entry", lambda e, relative, message: commits.append((e, relative, message))
                        or quarantine.render([e]))
    flaky.decide("orders-service")
    (kind, summary, labels, due, description), = created
    assert (kind, summary, labels, due) == ("Bug", "Flaky test: test_delivery_estimate_timeout (orders-service)",
                                            ["flaky-test"], "2026-11-16")
    assert "8 of 96 runs" in description and "https://stack.grafana.net/d/release-demo-flaky" in description
    assert "https://jenkins/77/" in description
    (written, relative, message), = commits
    assert message == "ORD-95 Quarantine test_delivery_estimate_timeout until 2026-11-16 [skip ci]"
    assert relative == "tests/quarantine.yaml" and written["until"] == datetime.date(2026, 11, 16)
    assert quarantine.load(tmp_path / "tests" / "quarantine.yaml")[0]["issue"] == "ORD-95"
    assert run_state.load()["test_lines"] == [
        "Flaky: test_delivery_estimate_timeout, 8 of 96 runs in 14 days. Quarantined until 16 Nov 2026. ORD-95."]


def test_decide_never_stops_a_release_when_grafana_doesnt_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")
    run_state.save({"service": "orders-service", "version": "1.5.0", "build": {"number": "1"}})
    monkeypatch.setattr(flaky, "ssm_client", lambda: None)
    monkeypatch.setattr(flaky, "get_parameter", lambda ssm, name: "https://stack.grafana.net")

    def down(*args):
        raise OSError("connection refused")

    monkeypatch.setattr(flaky, "grafana_counts", down)
    monkeypatch.setattr(sys, "argv", ["flaky.py", "decide", "orders-service"])
    assert flaky.main() == 0
    assert "Grafana didn't answer" in capsys.readouterr().out
    assert "test_lines" not in run_state.load()


def test_lines_go_to_slack_in_a_release_and_mark_the_tests_job_unstable(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(run_state, "REPORTS", reports)
    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")
    monkeypatch.setattr(junit_results, "read", lambda folder: results_of({"test_delivery_estimate_timeout": ("flaky", 2)}))
    run_state.save({"service": "orders-service", "slack": {"channel": "C1", "ts": ""}})
    flaky.lines("orders-service")
    assert (reports / "unstable.txt").read_text() == "Flaky: test_delivery_estimate_timeout failed, then passed on rerun.\n"

    import notify

    posted = []
    monkeypatch.setattr(notify, "post", lambda state: posted.append(list(state["lines"])))
    (reports / "unstable.txt").unlink()
    run_state.save({"service": "orders-service", "slack": {"channel": "C1", "ts": "1.2"}, "test_lines": ["Quarantined."]})
    flaky.lines("orders-service")
    assert posted == [["Quarantined.", "Flaky: test_delivery_estimate_timeout failed, then passed on rerun."]]
    assert not (reports / "unstable.txt").exists()


def test_the_change_attaches_the_attempts_only_after_a_rerun(tmp_path, monkeypatch):
    attached = []
    monkeypatch.setattr(change, "attach", lambda sn, change_id, path: attached.append(path.name))
    monkeypatch.setattr(run_state, "REPORTS", tmp_path)
    for name in ("junit.xml", "sbom.cdx.json", "grype.json", "build-info.json"):
        (tmp_path / name).write_text("{}")

    def evidence():
        attached.clear()
        for name in change.EVIDENCE:
            path = run_state.REPORTS / name
            if path.exists():
                change.attach(None, "id", path)
        return list(attached)

    assert evidence() == ["junit.xml", "sbom.cdx.json", "grype.json", "build-info.json"]
    (tmp_path / "junit-attempts.xml").write_text("<testsuites/>")
    assert evidence()[-1] == "junit-attempts.xml"


def test_the_main_videos_versions_run_clean_through_tests_run(tmp_path, runner):
    """1.4.0 passes with no rerun and no lines; 1.4.0-broken-test still stops at Test."""
    for state, expect in (("1.4.0", 0), ("1.4.0-broken-test", 1)):
        directory = tmp_path / state
        shutil.copytree(ROOT / "demo-states" / "orders-service" / state, directory)
        code, results = runner(directory)
        assert code == expect, state
        assert (runner.reports / "junit-attempts.xml").exists() == (expect == 1)
