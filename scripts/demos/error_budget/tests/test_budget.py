"""budget.py: the arithmetic on a recorded per-minute series, Slack's exact lines, and each way
through the stage. Fakes for CloudWatch, Jira, Slack and the events."""

import json

import pytest

import budget
import notify
import run_state
from conftest import (BUDGET_SUMMARY, NOW, SAM, FakeCloudWatch, FakeJira, FakeSession, budget_issue, release_state, story,
                      window)

AS_OF = budget.as_of_minute(NOW)
SLO = 99.9
SETTINGS = {"slo": 99.9, "window_hours": 24, "min_requests": 1000, "fix_max_changed_lines": 20,
            "issue_label": "error-budget", "function": "orders-service-production",
            "metrics": {"requests": "UrlRequestCount", "failed": "Errors"}, "reason_required": True}


@pytest.fixture
def world(monkeypatch):
    """Slack and the events recorded instead of sent, and the outside names the stage reads."""
    posted, events = [], []
    monkeypatch.setattr(notify, "post", lambda state: posted.append(json.loads(json.dumps(state["lines"]))))
    monkeypatch.setattr(budget, "send_event", events.append)
    monkeypatch.setattr(budget, "people", lambda: {"sam": {"display_name": "Sam Developer", "slack_user_id": SAM},
                                                   "alex": {"display_name": "Alex Change Manager", "slack_user_id": None}})
    monkeypatch.setattr(budget, "dashboard_address", lambda: "https://stack.example/d/release-demo-error-budget")
    return {"posted": posted, "events": events}


def reading_of(**kwargs):
    return budget.Reading(window(AS_OF, **kwargs), AS_OF, SLO, 24)


def stage(state, rows, jira=None, now=NOW, **kwargs):
    return budget.run_stage(state, "orders-service", session=FakeSession(FakeCloudWatch(rows, **kwargs)), jira=jira, now=now)


# The arithmetic.

def test_the_scripts_numbers():
    assert budget.allowed_of(86_412, SLO) == 86 and budget.spent_of(104, 86_412, SLO) == 120
    assert budget.allowed_of(86_455, SLO) == 86 and budget.spent_of(140, 86_455, SLO) == 161
    assert budget.spent_of(120, 86_412, SLO) == 138


def test_the_arithmetic_is_the_spec_integer_arithmetic_at_99_9():
    for requests in (1000, 1001, 3600, 19_800, 86_000, 86_400, 86_999):
        assert budget.allowed_of(requests, SLO) == requests // 1000
        for failed in (0, 1, 3, 86, 87, 105, 141):
            assert budget.spent_of(failed, requests, SLO) == failed * 100_000 // requests


def test_a_recorded_full_day_reproduces_requests_allowed_spent_and_the_back_time():
    # 86,412 requests; 1.27.0 live for 35 minutes up to the window's newest minute, 3 failures a minute.
    rows = window(AS_OF, extra=12, failing=(35, 1))
    assert rows[0][0] == AS_OF - 86_400 and rows[-1][0] == AS_OF - 60 and len(rows) == 1440
    reading = budget.Reading(rows, AS_OF, SLO, 24)
    assert (reading.requests, reading.failed, reading.allowed, reading.spent) == (86_412, 105, 86, 121)
    assert reading.over and reading.still_failing
    assert budget.back_about(reading, NOW) == ""  # while requests still fail, no forecast

    # Then the fix: 47 failing minutes, the last of them 10 minutes ago.
    fixed = budget.Reading(window(AS_OF, extra=55, failing=(57, 11)), AS_OF, SLO, 24)
    assert (fixed.requests, fixed.failed, fixed.allowed, fixed.spent) == (86_455, 141, 86, 163)
    assert not fixed.still_failing
    # 55 failures must leave: 19 failing minutes, the first of them 57 minutes before as-of,
    # plus 24 hours, plus the 3 minutes the check reads behind the clock.
    first_failure = AS_OF - 57 * 60
    assert fixed.back_at() == first_failure + 86_400 + 19 * 60 + 3 * 60
    assert budget.back_about(fixed, NOW) == " Back about 13:09 tomorrow."


def test_the_forecast_says_tomorrow_only_on_another_day():
    # A small budget that comes back in a few minutes: same day.
    rows = [(minute, 60, 0) for minute in range(AS_OF - 3600, AS_OF, 60)]
    rows[0] = (rows[0][0], 60, 10)
    reading = budget.Reading(rows, AS_OF, SLO, 1)
    assert reading.failed == 10 and reading.allowed == 3
    assert budget.back_about(reading, NOW) == " Back about 13:48."


def test_exactly_at_the_budget_nothing_is_refused():
    rows = window(AS_OF)
    rows[-20:-1] = [(minute, requests, 0) for minute, requests, _ in rows[-20:-1]]
    rows[100] = (rows[100][0], 60, 86)
    assert not budget.Reading(rows, AS_OF, SLO, 24).over
    rows[101] = (rows[101][0], 60, 1)
    assert budget.Reading(rows, AS_OF, SLO, 24).over


def test_the_window_ends_three_minutes_ago_on_the_minute():
    assert budget.clock(AS_OF) == "13:44"
    assert AS_OF % 60 == 0 and NOW - AS_OF == 200


def test_the_burn_rate_is_50_at_one_in_twenty_and_0_after_the_fix():
    assert budget.Reading(window(AS_OF, failing=(60, 1)), AS_OF, SLO, 24).burn_rate() == pytest.approx(50)
    assert budget.Reading(window(AS_OF, failing=(120, 61)), AS_OF, SLO, 24).burn_rate() == 0


def test_one_call_both_metrics_function_name_alone_and_missing_minutes_are_zero():
    rows = [row for row in window(AS_OF, failing=(5, 1)) if row[0] % 120 == 0]  # half the minutes are missing
    cloudwatch = FakeCloudWatch(rows)
    series = budget.read_series(cloudwatch, "orders-service-production", AS_OF, 24, SETTINGS["metrics"])
    assert len(cloudwatch.calls) == 1 and len(series) == 1440
    call = cloudwatch.calls[0]
    metrics = [query["MetricStat"]["Metric"] for query in call["MetricDataQueries"]]
    assert [metric["MetricName"] for metric in metrics] == ["UrlRequestCount", "Errors"]
    assert all(metric["Dimensions"] == [{"Name": "FunctionName", "Value": "orders-service-production"}] for metric in metrics)
    assert all(query["MetricStat"]["Period"] == 60 and query["MetricStat"]["Stat"] == "Sum" for query in call["MetricDataQueries"])
    assert call["StartTime"].timestamp() == AS_OF - 86_400 and call["EndTime"].timestamp() == AS_OF
    assert sum(1 for row in series if row[1] == 0) == 720


def test_every_page_of_the_answer_counts():
    rows = window(AS_OF, failing=(2000, 1))
    series = budget.read_series(FakeCloudWatch(rows, pages=2), "f", AS_OF, 24, SETTINGS["metrics"])
    assert sum(row[1] for row in series) == 86_400 and sum(row[2] for row in series) == 4320


def test_a_metric_cloudwatch_refuses_is_no_reading():
    with pytest.raises(RuntimeError, match="Forbidden"):
        budget.read_series(FakeCloudWatch(window(AS_OF), status="Forbidden"), "f", AS_OF, 24, SETTINGS["metrics"])


# Slack's lines, exact.

def test_within_the_budget_passes_with_the_three_lines(world):
    state = release_state(key="ORD-118", version="1.27.0", option="standard")
    assert stage(state, window(AS_OF, extra=12)) == 0
    assert state["lines"] == ["Error budget, last 24 hours to 13:44\n0 of 86,412 requests failed. 99.9% allows 86.\nSpent: 0%."]
    assert "force_normal" not in state and "override" not in state
    assert world["events"][0]["result"] == "within"


def test_too_few_requests_to_judge_passes(world):
    state = release_state()
    rows = [(minute, 1 if index < 999 else 0, 1 if index < 900 else 0) for index, minute in enumerate(range(AS_OF - 86_400, AS_OF, 60))]
    assert stage(state, rows) == 0
    assert state["lines"] == ["Error budget: 999 requests in the last 24 hours, too few to judge."]
    assert world["events"][0]["result"] == "too-few"


def test_a_refusal_while_failing_opens_one_issue_and_the_second_reuses_it(world):
    jira = FakeJira([story("ORD-120")], next_key=123)
    rows = window(AS_OF, extra=12, failing=(35, 1))
    state = release_state()
    assert stage(state, rows, jira) == budget.REFUSED
    assert state["lines"] == [
        "Error budget, last 24 hours to 13:44\n105 of 86,412 requests failed. 99.9% allows 86.\nSpent: 121%. Refused.",
        "The fix goes under ORD-123, opened now.\nTo ask Alex: /release orders-service ORD-120 override"]
    opened = jira.issues["ORD-123"]
    assert jira.created == ["ORD-123"] and opened["type"] == "Bug" and opened["labels"] == ["error-budget"]
    assert opened["summary"] == BUDGET_SUMMARY
    assert "105 of 86,412 requests failed" in opened["description"] and "/d/release-demo-error-budget" in opened["description"]
    assert jira.comments[0][0] == "ORD-120" and "refused" in jira.comments[0][1]
    assert world["events"][0]["result"] == "refused" and world["events"][0]["jira_key"] == "ORD-120"

    state = release_state()
    assert stage(state, rows, jira) == budget.REFUSED
    assert state["lines"][1] == "The fix goes under ORD-123.\nTo ask Alex: /release orders-service ORD-120 override"
    assert jira.created == ["ORD-123"] and len(jira.comments) == 2


def test_after_the_fix_a_refusal_says_when_and_opens_no_issue_from_the_fifth_minute_on(world):
    # ORD-123 closed with the fix's release. Nothing failed in the last 5 minutes: not the spec's 15,
    # or a refusal 6 minutes after the fix would open an issue its own line never names.
    jira = FakeJira([story("ORD-120"), budget_issue("ORD-123", status="Done")], next_key=124)
    state = release_state()
    assert stage(state, window(AS_OF, extra=55, failing=(52, 6)), jira) == budget.REFUSED
    assert state["lines"] == [
        "Error budget, last 24 hours to 13:44\n141 of 86,455 requests failed. 99.9% allows 86.\n"
        "Spent: 163%. Refused. Back about 13:14 tomorrow.",
        "To ask Alex: /release orders-service ORD-120 override"]
    assert jira.created == [] and jira.comments[0][0] == "ORD-120"


def test_the_budgets_own_issue_passes_with_a_small_change(world, monkeypatch):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: "a" * 40)
    monkeypatch.setattr(budget, "changed_lines", lambda commit, path: 1)
    jira = FakeJira([budget_issue("ORD-123")])
    state = release_state(key="ORD-123", version="1.27.1", option="standard")
    assert stage(state, window(AS_OF, extra=12, failing=(40, 1)), jira) == 0
    assert state["lines"] == ["Error budget, last 24 hours to 13:44\n120 of 86,412 requests failed. 99.9% allows 86.\nSpent: 138%.",
                              "ORD-123 passes: this budget's own issue,\n1 line of code changed since production."]
    assert state["change_work_notes"] == [
        "Error budget, last 24 hours to 13:44: 120 of 86,412 requests failed. 99.9% allows 86. Spent: 138%.",
        f"Passed as this budget's own issue, ORD-123. 1 line of code changed since production, {'a' * 7}..{'c' * 7}."]
    assert jira.comments == []
    assert world["events"][0]["result"] == "fix" and world["events"][0]["changed_lines"] == 1
    assert "force_normal" not in state


def test_the_budgets_own_issue_with_too_big_a_change_is_refused(world, monkeypatch):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: "a" * 40)
    monkeypatch.setattr(budget, "changed_lines", lambda commit, path: 34)
    jira = FakeJira([budget_issue("ORD-123")])
    state = release_state(key="ORD-123")
    assert stage(state, window(AS_OF, extra=12, failing=(40, 1)), jira) == budget.REFUSED
    assert state["lines"][0].endswith("Spent: 138%. Refused.")
    assert state["lines"][1] == ("34 lines of code changed since production.\nA fix may change 20.\n"
                                 "To ask Alex: /release orders-service ORD-123 override")
    assert world["events"][0]["result"] == "refused" and world["events"][0]["reason"] == "too big"


def test_twenty_changed_lines_is_still_a_fix_and_twenty_one_is_not(world, monkeypatch):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: "a" * 40)
    rows = window(AS_OF, extra=12, failing=(40, 1))
    for changed, code in ((20, 0), (21, budget.REFUSED)):
        monkeypatch.setattr(budget, "changed_lines", lambda commit, path, n=changed: n)
        assert stage(release_state(key="ORD-123"), rows, FakeJira([budget_issue("ORD-123")])) == code


def test_a_commit_that_cant_be_read_refuses_and_says_so(world, monkeypatch):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: None)
    state = release_state(key="ORD-123")
    assert stage(state, window(AS_OF, extra=12, failing=(40, 1)), FakeJira([budget_issue("ORD-123")])) == budget.REFUSED
    assert state["lines"][1] == ("Production's commit can't be read: no line count.\n"
                                 "To ask Alex: /release orders-service ORD-123 override")


@pytest.mark.parametrize("edit", [
    {"labels": ["orders-service", "error-budget"]},                     # acceptance check 7a: a Story with the label added
    {"labels": ["error-budget"], "type": "Bug"},                        # and its type changed: Release Pipeline reported it too
    {"labels": ["error-budget"], "type": "Bug", "reporter": "Mateusz"},
])
def test_only_the_issue_the_budget_opened_passes(world, monkeypatch, edit):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: "a" * 40)
    monkeypatch.setattr(budget, "changed_lines", lambda commit, path: 1)
    jira = FakeJira([story("ORD-120", **edit)], next_key=123)
    state = release_state(key="ORD-120")
    assert stage(state, window(AS_OF, extra=12, failing=(40, 1)), jira) == budget.REFUSED
    assert state["lines"][1].startswith("The fix goes under ORD-123, opened now.")


def test_a_closed_budget_issue_no_longer_passes(world, monkeypatch):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: "a" * 40)
    monkeypatch.setattr(budget, "changed_lines", lambda commit, path: 1)
    jira = FakeJira([budget_issue("ORD-123", status="Done")], next_key=124)
    assert stage(release_state(key="ORD-123"), window(AS_OF, extra=12, failing=(40, 1)), jira) == budget.REFUSED


def test_the_jql_asks_for_all_four_conditions():
    jira = FakeJira()
    budget.open_issues(jira, "orders-service", SETTINGS)
    jql = jira.asked[0]
    for part in ("project = ORD", "issuetype = Bug", 'labels = "error-budget"', "statusCategory != Done", "reporter = currentUser()"):
        assert part in jql


def test_override_asked_with_failures_going_on(world):
    jira = FakeJira([story("ORD-120")])
    state = release_state(option="override", version="1.28.1")
    assert stage(state, window(AS_OF, extra=55, failing=(47, 1)), jira) == 0
    assert state["lines"] == ["Error budget, last 24 hours to 13:44\n141 of 86,455 requests failed. 99.9% allows 86.\nSpent: 163%.",
                              "Override asked: Alex decides in ServiceNow."]
    assert state["override"] is True and jira.created == [] and jira.comments == []
    assert state["change_details"] == [
        "Error budget, last 24 hours to 13:44: 141 of 86,455 requests failed. 99.9% allows 86. Spent: 163%.",
        "Source: CloudWatch, UrlRequestCount and Errors, orders-service-production.",
        "Override asked by Sam Developer. While the budget is spent, only the budget's own issue releases without this approval."]
    assert state["change_attachments"] == [str(run_state.REPORTS / "error-budget.json")]
    assert world["events"][0]["result"] == "override-asked"


def test_override_asked_after_the_failures_stopped_says_when(world):
    state = release_state(option="override", version="1.28.1")
    assert stage(state, window(AS_OF, extra=55, failing=(52, 6)), FakeJira()) == 0
    assert state["lines"][0].endswith("Spent: 163%. Back about 13:14 tomorrow.")


def test_override_within_the_budget_is_a_plain_normal_change(world):
    state = release_state(option="override")
    assert stage(state, window(AS_OF, extra=12)) == 0
    assert "override" not in state and "change_details" not in state and world["events"][0]["result"] == "within"


def test_the_requester_is_never_shown_by_a_member_id(world, monkeypatch):
    def nobody():
        raise OSError("no SSM")

    monkeypatch.setattr(budget, "people", nobody)
    state = release_state(option="override", version="1.28.1")
    assert stage(state, window(AS_OF, extra=55, failing=(47, 1))) == 0
    assert SAM not in json.dumps(state["change_details"]) and "the person who asked" in state["change_details"][2]


# Toward a person, never silent.

def test_no_evidence_reader_is_no_reading_and_a_normal_change(world, monkeypatch):
    import evidence

    def refuse():
        raise PermissionError("AccessDenied")

    monkeypatch.setattr(evidence, "reader_session", refuse)
    state = release_state(option="standard")
    assert budget.run_stage(state, "orders-service", now=NOW) == 0
    line = "Error budget: no reading (the evidence reader role isn't available). Alex decides."
    assert state["lines"] == [line] and state["force_normal"] is True and state["change_details"] == [line]
    assert world["events"][0]["result"] == "no-reading"


def test_cloudwatch_down_is_no_reading(world):
    state = release_state(option="standard")
    assert stage(state, [], fails=OSError("throttled")) == 0
    assert state["lines"] == ["Error budget: no reading (CloudWatch didn't answer). Alex decides."]
    assert state["force_normal"] is True


def test_the_pipeline_can_say_no_reading_when_the_check_itself_broke(world):
    state = release_state()
    assert budget.main(["orders-service", "--no-reading", "the check itself failed, see the build log"]) == 0
    state = run_state.load()
    assert state["lines"] == ["Error budget: no reading (the check itself failed, see the build log). Alex decides."]


def test_jira_down_the_refusal_stands_and_the_line_says_no_issue_was_opened(world):
    state = release_state()
    assert stage(state, window(AS_OF, extra=12, failing=(40, 1)), FakeJira(down=True)) == budget.REFUSED
    assert state["lines"][1] == "No issue opened: Jira didn't answer.\nTo ask Alex: /release orders-service ORD-120 override"


def test_jira_refusing_only_the_new_issue_says_the_same(world):
    jira = FakeJira()

    def refuse(*args, **kwargs):
        raise SystemExit("Jira POST: 400")

    jira.create = refuse
    state = release_state()
    assert stage(state, window(AS_OF, extra=12, failing=(40, 1)), jira) == budget.REFUSED
    assert state["lines"][1].startswith("No issue opened: Jira didn't answer.")


def test_slack_or_the_events_failing_changes_nothing_decided(world, monkeypatch):
    def broken(*args):
        raise OSError("down")

    monkeypatch.setattr(notify, "post", broken)
    monkeypatch.setattr(budget, "send_event", broken)
    assert stage(release_state(), window(AS_OF, extra=12, failing=(40, 1)), FakeJira()) == budget.REFUSED


# The record.

def test_the_report_holds_the_series_the_minute_the_values_and_the_decision(world):
    stage(release_state(), window(AS_OF, extra=12, failing=(35, 1)), FakeJira())
    report = json.loads((run_state.REPORTS / "error-budget.json").read_text())
    assert report["result"] == "refused" and report["as_of"] == run_state.iso(AS_OF)
    assert (report["requests"], report["failed"], report["allowed"], report["spent"]) == (86_412, 105, 86, 121)
    assert report["slo"] == 99.9 and report["window_hours"] == 24 and report["burn_rate_last_hour"] == pytest.approx(29.17, abs=0.01)
    assert len(report["series"]) == 1440 and report["series"][-1][2] == 3
    assert report["metrics"] == {"requests": "UrlRequestCount", "failed": "Errors"}


def test_the_event_has_its_own_fields_and_no_result_the_releases_table_would_count(world):
    stage(release_state(), window(AS_OF, extra=12, failing=(35, 1)), FakeJira())
    event = world["events"][0]
    for name in ("requests", "failed", "allowed", "spent", "slo", "window_hours", "as_of", "jira_key", "result"):
        assert event[name] is not None, name
    assert event["result"] != "failed" and event["stage"] == "Error budget" and event["event"] == "error_budget"


def test_every_line_fits_a_4_to_5_crop(world, monkeypatch):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: "a" * 40)
    longest = []
    for key, failing, issues in (("ORD-120", (35, 1), []), ("ORD-120", (57, 6), []), ("ORD-123", (40, 1), [budget_issue("ORD-123")])):
        for changed in (1, 34):
            monkeypatch.setattr(budget, "changed_lines", lambda commit, path, n=changed: n)
            state = release_state(key=key)
            stage(state, window(AS_OF, extra=412, failing=failing), FakeJira(issues, next_key=1000))
            longest += [line for block in state["lines"] for line in block.split("\n")]
    assert max(len(line) for line in longest) <= 53, max(longest, key=len)


def test_the_add_stage_puts_the_row_first_once():
    state = release_state()
    budget.add_stage(state)
    budget.add_stage(state)
    assert list(run_state.load()["stages"])[:2] == ["Error budget", "Build"]
    assert list(run_state.load()["stages"]).count("Error budget") == 1


# After Alex's approval.

class FakeChange:
    REJECTION_COMMENT_LIMIT = 120

    def __init__(self, comment):
        self.comment = comment

    def connect(self):
        return "sn", None

    def approved_comment(self, sn, change):
        return self.comment


def approved_state(**extra):
    return release_state(option="override", version="1.28.1", override=True,
                         change={"sys_id": "c1", "number": "CHG0030090", "type": "normal", "approved_by": "Alex"},
                         error_budget={"result": "override-asked", "requests": 86_455, "failed": 140, "spent": 161}, **extra)


def test_approved_does_nothing_unless_this_run_is_an_override(world):
    state = release_state(option="override")
    assert budget.approved(state) == 0 and budget.released(state) == 0
    assert world["posted"] == [] and "close_notes_extra" not in state


def test_an_approval_with_a_comment_goes_on_the_record(world, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "change", FakeChange("The fix is live and failures stopped. The Monday campaign needs this."))
    state = approved_state()
    assert budget.approved(state) == 0
    assert state["lines"][-1] == "Override reason: The fix is live and failures stopped. The Monday campaign needs this."
    assert state["close_notes_extra"] == ("Released with the error budget 161% spent. Override approved by Alex: "
                                          "The fix is live and failures stopped. The Monday campaign needs this.")
    event = world["events"][-1]
    assert event["result"] == "override-approved" and event["approver"] == "Alex" and event["change"] == "CHG0030090"
    assert event["comment"].startswith("The fix is live") and event["spent"] == 161


def test_an_approval_with_no_comment_stops_the_release(world, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "change", FakeChange(""))
    state = approved_state()
    assert budget.approved(state) == budget.REFUSED
    assert state["lines"][-1] == "Override refused: no reason in Alex's approval."
    assert "close_notes_extra" not in state and world["events"] == []


def test_a_comment_that_cant_be_read_stops_it_too(world, monkeypatch):
    import sys

    class Broken(FakeChange):
        def approved_comment(self, sn, change):
            raise OSError("ServiceNow is asleep")

    monkeypatch.setitem(sys.modules, "change", Broken(""))
    assert budget.approved(approved_state()) == budget.REFUSED


def test_without_the_reason_rule_his_name_is_enough(world, monkeypatch):
    # The script's fallback, if a comment can't be read from an approved record.
    import sys

    monkeypatch.setitem(sys.modules, "change", FakeChange(""))
    monkeypatch.setattr(budget, "settings_of", lambda name: {**SETTINGS, "reason_required": False})
    state = approved_state()
    assert budget.approved(state) == 0
    assert state["close_notes_extra"] == "Released with the error budget 161% spent. Override approved by Alex."


def test_a_long_reason_is_cut_in_slack_but_not_in_the_close_notes(world, monkeypatch):
    import sys

    reason = "x" * 200
    monkeypatch.setitem(sys.modules, "change", FakeChange(reason))
    state = approved_state()
    assert budget.approved(state) == 0
    assert state["lines"][-1] == "Override reason: " + "x" * 117 + "..."
    assert state["close_notes_extra"].endswith(reason)


def test_released_marks_the_chart_with_its_own_tags(world, monkeypatch):
    import events

    marked = []
    monkeypatch.setattr(events, "annotate", lambda ssm, text, tags: marked.append((text, tags)) or 200)
    monkeypatch.setattr(budget, "ssm_client", lambda: None)
    assert budget.released(approved_state()) == 0
    assert marked == [("orders-service 1.28.1 released with the error budget spent, override by Alex (ORD-120)",
                       ["error-budget", "override", "orders-service"])]


# Status and the address.

def test_status_prints_the_numbers_and_the_decision_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(budget, "production_commit", lambda service, function: "a" * 40)
    monkeypatch.setattr(budget, "changed_lines", lambda commit, path: 34)
    out = []
    jira = FakeJira([budget_issue("ORD-123")])
    session = FakeSession(FakeCloudWatch(window(AS_OF, extra=12, failing=(35, 1))))
    assert budget.status("orders-service", "ORD-120", "", out=out.append, session=session, jira=jira, now=NOW) == 0
    text = "\n".join(out)
    assert "105 of 86,412 requests failed. 99.9% allows 86." in text and "Spent: 121%." in text
    assert "Burn rate, last hour: 29.2x." in text and "Open budget issues: ORD-123." in text
    assert "Decision for ORD-120: refused." in text
    out.clear()
    assert budget.status("orders-service", "ORD-123", "standard", out=out.append, session=session, jira=jira, now=NOW) == 0
    assert "Decision for ORD-123, option standard: too-big (34 lines of code changed since production.)." in "\n".join(out)
    assert jira.created == [] and jira.comments == [] and not run_state.PATH.exists()


def test_status_still_gives_the_numbers_with_jira_down():
    out = []
    session = FakeSession(FakeCloudWatch(window(AS_OF, extra=12, failing=(35, 1))))
    budget.status("orders-service", None, "", out=out.append, session=session, jira=FakeJira(down=True), now=NOW)
    assert "Jira not read (OSError), so no budget issue counts." in out
    assert out[-1] == "Decision for another key: refused."


def test_status_reads_with_your_sign_in_from_the_mac_which_the_reader_role_doesnt_trust(monkeypatch):
    import common
    import host

    monkeypatch.setattr(host, "on_host", lambda: False)
    monkeypatch.setattr(common, "aws_session", lambda: "your sign-in")
    assert budget.status_session() == "your sign-in"


def test_the_address_is_pinned_to_the_window_a_second_short_of_its_last_minute():
    url = budget.dashboard_url("https://stack.example/", 24, AS_OF)
    assert url.startswith("https://stack.example/d/release-demo-error-budget/error-budget?orgId=1&from=")
    assert f"from={(AS_OF - 86_400) * 1000}&to={AS_OF * 1000 - 1000}" in url
    assert budget.dashboard_url("https://stack.example", 24) == "https://stack.example/d/release-demo-error-budget/error-budget?orgId=1"


def test_as_of_hh_mm_is_todays_or_yesterdays_if_still_to_come():
    assert budget.pinned_minute("13:44", NOW) == AS_OF
    assert budget.pinned_minute("13:48", NOW) == AS_OF + 4 * 60 - 86_400


def test_changed_lines_leaves_out_tests_and_version(monkeypatch):
    import subprocess

    import evidence

    numstat = "1\t1\tservices/orders-service/VERSION\n1\t1\tservices/orders-service/handler.py\n40\t0\tservices/orders-service/tests/test_handler.py\n"
    monkeypatch.setattr(evidence, "has_commit", lambda commit: True)
    monkeypatch.setattr(evidence, "git", lambda *args: subprocess.CompletedProcess(args, 0, numstat, ""))
    assert budget.changed_lines("a" * 40, "services/orders-service") == 1
    monkeypatch.setattr(evidence, "has_commit", lambda commit: False)
    assert budget.changed_lines("initial", "services/orders-service") is None
