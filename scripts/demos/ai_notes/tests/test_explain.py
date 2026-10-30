"""Video 13's reply: the CloudWatch lines it reads, the quotes it keeps, its
fixed form, and what reaches the API."""

import json
import shutil

import pytest

import explain
import llm
import notes
from conftest import Recorder, no_findings, sdk_response
from test_verify import CHECK, QUOTES

FAILED = {"function": "orders-service-production", "lambda_version": "58", "rolled_back_to": "1.4.0"}
EVENTS = [
    {"timestamp": 1793000001000, "message": "START RequestId: 0d2a Version: 58"},
    {"timestamp": 1793000001200, "message": "LAMBDA_WARNING: Unhandled exception. The most likely cause is an issue "
                                            "in the function code."},
    {"timestamp": 1793000001300, "message": "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAYS'\nTraceback (most recent call "
                                            "last):\n  File \"/var/task/handler.py\", line 84, in delivery_estimate_days"
                                            "\n    return int(os.environ[\"DELIVERY_ESTIMATE_DAYS\"])\n"},
    {"timestamp": 1793000001400, "message": "END RequestId: 0d2a"},
]
CAUSE = {"cause": "1.4.1 reads DELIVERY_ESTIMATE_DAYS, which production doesn't define: production has DELIVERY_DAYS.",
         "evidence": QUOTES, "not_checked": ["whether other settings were renamed", "staging's logs"]}
REPLY = """*Likely cause of the failed production check*
Written by claude-opus-5-5 from this run's logs, diff and settings. Each quote checked word for word by script.

1.4.1 reads DELIVERY_ESTIMATE_DAYS, which production doesn't define: production has DELIVERY_DAYS.

*Evidence*
• CloudWatch, orders-service-production version 58: `[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAYS'`
• Diff 1.4.0 to 1.4.1, handler.py: `return int(os.environ["DELIVERY_ESTIMATE_DAYS"])`
• Settings, production: `DELIVERY_DAYS, ENVIRONMENT`
Quotes found in their sources: 3 of 3.

*Not checked:* whether other settings were renamed; staging's logs."""
BUILD = "https://jenkins-demo.es.nl/job/release/62/"


def run(evidence, data, scan=no_findings, events=EVENTS):
    recorder = Recorder(sdk_response(data))
    text, answer = explain.explain(evidence, CHECK, explain.ordered_lines(events, 200), FAILED, BUILD,
                                   client=recorder.client(), scan=scan)
    return text, recorder


def test_error_lines_and_their_traceback_come_first():
    lines = explain.ordered_lines(EVENTS, 200)
    assert lines[:4] == ["LAMBDA_WARNING: Unhandled exception. The most likely cause is an issue in the function code.",
                         "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAYS'", "Traceback (most recent call last):",
                         '  File "/var/task/handler.py", line 84, in delivery_estimate_days']
    assert lines[-2:] == ["START RequestId: 0d2a Version: 58", "END RequestId: 0d2a"]
    assert len(explain.ordered_lines(EVENTS, 3)) == 3


def test_the_streams_of_a_version_by_day():
    assert explain.stream_prefixes("58", 1793000000, 1793000100) == ["2026/10/26/[58]"]
    # A rollback just after midnight UTC reads both days' streams.
    assert explain.stream_prefixes("7", 1793059000, 1793059000 + 7200) == ["2026/10/26/[7]", "2026/10/27/[7]"]


class FakeLogs:
    """DescribeLogStreams and FilterLogEvents, where the error line arrives late."""

    def __init__(self, arrive_after):
        self.calls, self.arrive_after, self.asked = 0, arrive_after, []

    def get_paginator(self, name):
        logs = self

        class Paginator:
            def paginate(self, **kwargs):
                logs.asked.append((name, kwargs))
                if name == "describe_log_streams":
                    yield {"logStreams": [{"logStreamName": kwargs["logStreamNamePrefix"] + "abc"}]}
                else:
                    logs.calls += 1
                    yield {"events": EVENTS if logs.calls > logs.arrive_after else EVENTS[:1]}
        return Paginator()


def test_it_waits_for_the_first_error_line_up_to_twenty_seconds():
    clock = {"now": 0.0}
    logs = FakeLogs(arrive_after=3)
    events = explain.log_events(logs, "/aws/lambda/orders-service-production", "58", 1793000000, 1793000130,
                                sleep=lambda s: clock.update(now=clock["now"] + s), clock=lambda: clock["now"])
    assert logs.calls == 4 and len(events) == 4 and clock["now"] == 6
    name, kwargs = logs.asked[1]
    assert kwargs["logStreamNames"] == ["2026/10/26/[58]abc"]
    assert (kwargs["startTime"], kwargs["endTime"]) == (1793000000000, 1793000130000)

    clock["now"], late = 0.0, FakeLogs(arrive_after=100)
    events = explain.log_events(late, "/aws/lambda/orders-service-production", "58", 1793000000, 1793000130,
                                sleep=lambda s: clock.update(now=clock["now"] + s), clock=lambda: clock["now"])
    assert clock["now"] == 20 and len(events) == 1


def test_the_reply_in_its_fixed_form(take_evidence):
    text, recorder = run(take_evidence, CAUSE)
    assert text == REPLY
    body = recorder.requests[0]["body"]
    assert body["output_config"]["format"]["schema"] == explain.SCHEMA
    assert "fix" not in json.dumps(explain.SCHEMA)
    sent = json.loads(body["messages"][0]["content"])
    assert sent["settings"] == ["staging: DELIVERY_ESTIMATE_DAYS, ENVIRONMENT", "production: DELIVERY_DAYS, ENVIRONMENT"]


def test_the_same_words_go_to_the_change_and_jira():
    assert notes.plain(REPLY).splitlines()[:1] == ["Likely cause of the failed production check"]
    assert "- Settings, production: DELIVERY_DAYS, ENVIRONMENT" in notes.plain(REPLY)
    assert "Not checked: whether other settings were renamed; staging's logs." in notes.plain(REPLY)


def test_a_quote_off_by_one_character_is_dropped_and_named(take_evidence):
    data = {**CAUSE, "evidence": [{"source": "cloudwatch", "quote": "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAY'"},
                                  *QUOTES[1:]]}
    text, _ = run(take_evidence, data)
    assert "Quotes found in their sources: 2 of 3. Dropped: CloudWatch, not found." in text
    assert "• CloudWatch" not in text


def test_no_quote_found_gives_no_cause(take_evidence):
    data = {**CAUSE, "evidence": [{"source": "check", "quote": "FAIL / answered 503"}]}
    text, _ = run(take_evidence, data)
    assert text == ("No likely cause from the logs this time: none of its quotes was found in its source. The check's "
                    f"own output is in the build: {BUILD}artifact/.release/check-production.txt")


def test_a_cause_that_names_a_setting_outside_the_sources_is_refused(take_evidence):
    text, _ = run(take_evidence, {**CAUSE, "cause": "1.4.1 reads REGIONAL_DELIVERY_DAYS."})
    assert text.startswith("No likely cause from the logs this time: the cause named setting REGIONAL_DELIVERY_DAYS")


def test_an_api_failure_gives_no_cause_with_the_reason(take_evidence):
    recorder = Recorder(status=500)
    text, answer = explain.explain(take_evidence, CHECK, [], FAILED, BUILD, client=recorder.client(), scan=no_findings)
    assert text.startswith("No likely cause from the logs this time: the API answered 500.") and answer is None


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="needs gitleaks 8.30.1 on the PATH")
def test_a_fake_aws_key_in_a_saved_log_line_reaches_the_api_as_removed(take_evidence, monkeypatch):
    if llm.gitleaks_command() is None:
        pytest.skip("gitleaks here isn't the pinned version")
    fake_key = "AKIA" + "QWERTYUIOPASDFGH"  # split, so this file holds no key gitleaks would flag
    events = [*EVENTS, {"timestamp": 1793000001500, "message": f"retrying with key {fake_key}"}]
    text, recorder = run(take_evidence, CAUSE, scan=llm.gitleaks_findings, events=events)
    content = recorder.requests[0]["body"]["messages"][0]["content"]
    assert fake_key not in content and "retrying with key [removed]" in content


def test_from_files_runs_the_same_task_and_writes_nowhere(take_evidence, tmp_path, monkeypatch, capsys):
    explain.save_sources(tmp_path / "sources", take_evidence, CHECK, EVENTS, FAILED)
    recorder = Recorder(sdk_response(CAUSE))
    monkeypatch.setattr(explain, "ask", lambda *args, **kwargs: llm.ask(*args, client=recorder.client(),
                                                                        scan=no_findings))
    monkeypatch.setattr("sys.argv", ["explain.py", "--from-files", str(tmp_path / "sources"), "--build-url", BUILD])
    for writer in ("slack_thread_reply", "work_note", "jira_comment", "send_event", "notes_servicenow"):
        monkeypatch.setattr(notes, writer, lambda *a, **k: pytest.fail("from-files wrote somewhere"))
    assert explain.main() == 0
    assert capsys.readouterr().out.strip() == REPLY
