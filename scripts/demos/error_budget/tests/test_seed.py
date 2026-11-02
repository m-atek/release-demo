"""seed.py: the quiet day before a take is seeded, and only that. Every failure counted stays real."""

import datetime
import importlib.util
import json

import budget
import pytest
from common import service
from conftest import HERE, NOW, FakeCloudWatch, FakeJira, FakeSession, release_state, window

SETTINGS = service("orders-service")["error_budget"]
AS_OF = budget.as_of_minute(NOW)
DAY = 1440


def load_seed():
    spec = importlib.util.spec_from_file_location("budget_seed", HERE / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = load_seed()


def seeded_day(now=NOW):
    return {minute: seed.count_for(minute) for minute in seed.minutes_to_seed(now, 24)}


# The numbers it writes.

def test_a_seeded_day_is_a_full_day_slightly_uneven_with_no_failed_metric():
    counts = [seed.count_for(minute) for minute in range(AS_OF - 86400, AS_OF, 60)]
    assert set(counts) == {59, 60, 61} and 86_000 <= sum(counts) <= 86_999 and 86_400 - 100 < sum(counts) < 86_400 + 100
    assert sum(1 for count in counts if count != 60) > 200
    assert seed.count_for(AS_OF) == seed.count_for(AS_OF)


def test_no_minute_it_writes_is_24_hours_old_and_none_is_the_minute_now():
    minutes = seed.minutes_to_seed(NOW, 24)
    assert len(minutes) == DAY - seed.MARGIN_MINUTES
    assert all(minute % 60 == 0 for minute in minutes)
    assert NOW - minutes[0] < 24 * 3600
    assert minutes[-1] + 60 == int(NOW // 60) * 60


def test_it_writes_one_metric_of_its_own_in_batches():
    cloudwatch = FakeCloudWatch()
    minutes = seed.minutes_to_seed(NOW, 24)
    seed.write(cloudwatch, SETTINGS, minutes)
    assert {namespace for namespace, _ in cloudwatch.written} == {"ReleaseDemo/ErrorBudget"}
    data = [datum for _, batch in cloudwatch.written for datum in batch]
    assert len(data) == len(minutes) and all(len(batch) <= seed.BATCH for _, batch in cloudwatch.written)
    assert {datum["MetricName"] for datum in data} == {"SeededRequests"}
    assert all(datum["Dimensions"] == [{"Name": "FunctionName", "Value": "orders-service-production"}] for datum in data)
    assert all(datum["Timestamp"].tzinfo == datetime.timezone.utc and datum["Unit"] == "Count" for datum in data)


def test_a_second_run_changes_nothing_it_wrote():
    cloudwatch = FakeCloudWatch()
    minutes = seed.minutes_to_seed(NOW, 24)
    seed.write(cloudwatch, SETTINGS, minutes)
    first = dict(cloudwatch.seeded)
    seed.write(cloudwatch, SETTINGS, minutes)
    assert cloudwatch.seeded == first


def test_a_later_run_adds_only_the_minutes_since(monkeypatch):
    cloudwatch = FakeCloudWatch()
    seed.write(cloudwatch, SETTINGS, seed.minutes_to_seed(NOW, 24))
    first = dict(cloudwatch.seeded)
    seed.write(cloudwatch, SETTINGS, seed.minutes_to_seed(NOW + 3000, 24))
    assert {minute: cloudwatch.seeded[minute] for minute in first} == first
    assert len(cloudwatch.seeded) == len(first) + 50


def test_the_dry_run_writes_nothing(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(seed, "aws_session", lambda: called.append("session"))
    monkeypatch.setattr("sys.argv", ["seed.py", "--dry-run"])
    assert seed.main() == 0
    out = capsys.readouterr().out
    assert called == [] and "Would seed 1,437 minutes" in out and "none failed" in out and "SeededRequests" in out


# Every minute once.

def test_a_seeded_minute_reads_its_seed_and_0_failed_never_both():
    # Real traffic and real failures in a seeded minute don't add to it: today's other videos, an earlier take.
    rows = [(AS_OF - 180, 7, 20), (AS_OF - 120, 60, 0), (AS_OF - 60, 60, 3)]
    cloudwatch = FakeCloudWatch(rows, seeded={AS_OF - 180: 61})
    series = budget.read_series(cloudwatch, "f", AS_OF, 1, SETTINGS["metrics"], SETTINGS["seed"])
    assert series[-3:] == [(AS_OF - 180, 61, 0), (AS_OF - 120, 60, 0), (AS_OF - 60, 60, 3)]


def test_the_seed_is_read_in_the_same_call_with_maximum():
    cloudwatch = FakeCloudWatch()
    budget.read_series(cloudwatch, "orders-service-production", AS_OF, 24, SETTINGS["metrics"], SETTINGS["seed"])
    assert len(cloudwatch.calls) == 1
    seeded = cloudwatch.calls[0]["MetricDataQueries"][2]
    assert seeded["Id"] == "seeded" and seeded["MetricStat"]["Stat"] == "Maximum" and seeded["MetricStat"]["Period"] == 60
    assert seeded["MetricStat"]["Metric"]["Namespace"] == "ReleaseDemo/ErrorBudget"


def test_with_no_seed_setting_it_reads_aws_lambda_alone():
    cloudwatch = FakeCloudWatch(window(AS_OF, failing=(30, 1)), seeded={AS_OF - 60: 61})
    reading = budget.take_reading(cloudwatch, {**SETTINGS, "seed": None}, NOW)
    assert reading.requests == 86_400 and reading.failed == 90 and reading.seeded == []
    assert len(cloudwatch.calls[0]["MetricDataQueries"]) == 2


def take_day(burning_minutes=0, seeded_until=None):
    """A seeded day up to seeded_until, then real minutes at 60 with 3 failed for the last burning_minutes."""
    seeded_until = seeded_until or AS_OF - 45 * 60
    seeded = {minute: seed.count_for(minute) for minute in range(AS_OF - 86400 - 3000, seeded_until, 60)}
    rows = [row for row in window(AS_OF, failing=(burning_minutes, 1) if burning_minutes else None) if row[0] >= seeded_until]
    return FakeCloudWatch(rows, seeded=seeded)


def test_minute_0_reads_a_full_day_and_failed_0_though_today_had_real_failures():
    # 31 real failures at 14:06 stay under the seed, and the report says so.
    cloudwatch = take_day()
    cloudwatch.rows += [(AS_OF - 120 * 60, 700, 31)]
    reading = budget.take_reading(cloudwatch, SETTINGS, NOW)
    assert 86_000 <= reading.requests <= 86_999 and reading.failed == 0 and reading.allowed == 86
    assert reading.under_seed == 31 and reading.seeded_until == AS_OF - 45 * 60
    assert budget.clock(reading.seeded_until) in budget.seeded_line(reading) and "hides: 31" in budget.seeded_line(reading)


def test_take_a_counts_only_the_real_failures_and_refuses():
    cloudwatch = take_day(burning_minutes=35)
    reading = budget.take_reading(cloudwatch, SETTINGS, NOW)
    assert reading.failed == 105 and reading.allowed == 86 and reading.over and reading.still_failing
    assert 86_000 <= reading.requests <= 86_999
    assert reading.spent == reading.failed * 100000 // reading.requests
    assert len(reading.seeded) == DAY - 45


# Saying so.

def world(cloudwatch):
    jira = FakeJira([])
    return FakeSession(cloudwatch), jira


@pytest.fixture
def quiet(monkeypatch):
    import notify

    monkeypatch.setattr(budget, "people", lambda: {"sam": {"slack_user_id": "U0SAM000001", "display_name": "Sam Developer"}})
    monkeypatch.setattr(budget, "send_event", lambda body: None)
    monkeypatch.setattr(notify, "post", lambda state: None)


def test_the_override_s_source_line_says_what_is_seeded(quiet):
    state = release_state(option="override", version="1.28.1")
    session, jira = world(take_day(burning_minutes=35))
    assert budget.run_stage(state, "orders-service", session=session, jira=jira, now=NOW) == 0
    until = budget.clock(AS_OF - 45 * 60)
    assert state["change_details"][1] == (f"Source: CloudWatch, UrlRequestCount and Errors, orders-service-production. "
                                          f"Requests before {until} are seeded, none failed.")


def test_with_nothing_seeded_the_source_line_says_nothing_of_it(quiet):
    state = release_state(option="override", version="1.28.1")
    session, jira = world(FakeCloudWatch(window(AS_OF, failing=(35, 1))))
    assert budget.run_stage(state, "orders-service", session=session, jira=jira, now=NOW) == 0
    assert state["change_details"][1] == "Source: CloudWatch, UrlRequestCount and Errors, orders-service-production."


def test_the_report_and_the_event_say_what_is_seeded(quiet, monkeypatch):
    events = []
    monkeypatch.setattr(budget, "send_event", events.append)
    state = release_state()
    session, jira = world(take_day(burning_minutes=35))
    assert budget.run_stage(state, "orders-service", session=session, jira=jira, now=NOW) == budget.REFUSED
    report = json.loads(budget.report_path().read_text())
    assert report["seeded_minutes"] == DAY - 45 and report["seeded_until"] and report["real_failed_under_the_seed"] == 0
    assert events[0]["seeded_minutes"] == DAY - 45 and events[0]["seeded_until"] == report["seeded_until"]


def test_slack_s_lines_don_t_change_with_a_seed(quiet):
    state = release_state()
    session, jira = world(take_day(burning_minutes=35))
    budget.run_stage(state, "orders-service", session=session, jira=jira, now=NOW)
    assert "seed" not in " ".join(state["lines"]).lower()
    assert all(len(line) <= 53 for block in state["lines"] for line in block.splitlines())


def test_status_says_what_is_seeded():
    lines = []
    budget.status("orders-service", "ORD-1", "", out=lines.append, session=FakeSession(take_day()), jira=FakeJira([]), now=NOW)
    assert any(line.startswith("Seeded: 1,395 minutes of this window") for line in lines)


def test_check_waits_for_a_full_day():
    lines = []
    assert seed.check(take_day(), SETTINGS, NOW, out=lines.append) == 0 and lines[-1].startswith("Ready")
    lines.clear()
    assert seed.check(FakeCloudWatch(window(AS_OF, per_minute=1)), SETTINGS, NOW, out=lines.append) == 1
    assert lines[-1].startswith("Not yet")
    lines.clear()
    cloudwatch = take_day(burning_minutes=2)
    assert seed.check(cloudwatch, SETTINGS, NOW, out=lines.append) == 1


def test_check_won_t_take_the_seed_s_word_for_the_traffic(monkeypatch):
    import demo

    # Seeded up to the newest minute, with nothing sent: the seeded minute reads 60, and the check still says FAIL.
    cloudwatch = FakeCloudWatch(seeded={minute: seed.count_for(minute) for minute in range(AS_OF - 86400, AS_OF, 60)})
    assert budget.take_reading(cloudwatch, SETTINGS, NOW).series[-1][1] in range(59, 62)
    assert demo.last_real_minute(cloudwatch, SETTINGS, AS_OF)[1:] == (0, 0)


# The right to write it.

def test_the_tools_host_may_write_this_namespace_and_no_other():
    text = (HERE.parents[2] / "infra" / "demos" / "27-error-budget" / "main.tf").read_text()
    statement = text[text.index('sid       = "WriteTheSeededFill"'):]
    assert 'actions   = ["cloudwatch:PutMetricData"]' in statement and 'variable = "cloudwatch:namespace"' in statement
    assert 'values   = [try(local.budget.seed.namespace, "ReleaseDemo/ErrorBudget")]' in statement
    assert not SETTINGS["seed"]["namespace"].startswith("AWS/")
