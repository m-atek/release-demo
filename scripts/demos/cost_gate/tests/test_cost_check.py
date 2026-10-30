"""Video 18's cost check: the arithmetic to the cent (acceptance check 6),
the measurement from Lambda's own lines (7), a person instead of a stop (8),
and the lines Slack and the change show."""

import json

import pytest

import cost_check
import costs
import publish
from conftest import ORDERS, FakeLambda, FakeLogs, FakeReader, claude_line, report, store_line

SETTINGS = {"limit_usd_month": 100, "approvers": "Cost approvers",
            "sample": {"page_views": 50, "path": "/", "concurrency": 5},
            "expected_month": {"page_views": 3_000_000, "new_orders": 60_000}}
IDS = [f"7c0e{n:04d}-aaaa-4bbb-8ccc-0123456789ab" for n in range(50)]


def state(version, option="standard"):
    return {"service": "orders-service", "version": version, "option": option, "jira_key": "ORD-55",
            "build": {"number": "70", "url": "https://jenkins-demo.es.nl/job/release/70/"}, "commit": "c" * 40,
            "deploys": {"staging": {"url": "https://staging.lambda-url.example/"}},
            "package": {"path": f"orders-service-candidates/{version}/70/orders-service-{version}.zip"},
            "stages": {"Check staging": {"status": "ok"}, "Change": {"status": "waiting"}}, "slack": {}}


def fetcher(ids=IDS, statuses=None):
    answers = iter(zip(ids, statuses or [200] * len(ids)))
    return lambda url: next(answers)


def logs_170():
    return [line for n, request_id in enumerate(IDS)
            for line in (claude_line(request_id, "1.7.0", "page_view"), report(request_id, 6800 + n))]


def logs_171(first_call=True):
    lines = []
    for n, request_id in enumerate(IDS):
        if n == 0 and first_call:
            lines.append(claude_line(request_id, "1.7.1", "order", model="claude-haiku-5-5", tokens_in=1050,
                                     tokens_out=400))
            lines.append(store_line(request_id, "1.7.1", 1.5, 5.0))
        else:
            lines.append(store_line(request_id, "1.7.1", 1.5, 0.0))
        lines.append(report(request_id, 40 + n % 3))
    return lines


@pytest.fixture
def production(monkeypatch):
    """Production on 1.4.0, whose release copy holds the cost.json of its own check."""
    measured = {"build": "64", "per_page_view": {"total_usd": 0.000001}, "per_new_order": {"total_usd": 0.0}}
    files = {"orders-service-releases/1.4.0/build-info.json": json.dumps({"sha256": "68656c6c6f"}).encode(),
             "orders-service-releases/1.4.0/cost.json": json.dumps(measured).encode()}

    def request(method, path, token, data=None, headers=None):
        if path == "api/storage/orders-service-releases/1.4.0":
            return 200, json.dumps({"children": [{"uri": "/build-info.json", "folder": False}]}).encode()
        return (200, files[path]) if path in files else (404, b"")
    monkeypatch.setattr(publish, "request", request)
    return files


def run(version, messages, production_lambda=None, **kwargs):
    reader = FakeReader(FakeLogs(messages, **kwargs), production_lambda)
    return cost_check.check(state(version), SETTINGS, reader, fetch=fetcher(), logs_wait=30, jcr_token="token",
                            sleep=lambda s: None)


def test_one_seven_zero_is_measured_from_lambdas_own_lines_and_priced_to_the_cent(production):
    record = run("1.7.0", logs_170())
    page = record["per_page_view"]
    # Lambda's REPORT lines and the function's own lines, request by request.
    assert {row["request_id"]: row["billed_ms"] for row in record["requests"]} == {
        request_id: 6800 + n for n, request_id in enumerate(IDS)}
    assert all(row["claude"][0]["input_tokens"] == 1512 for row in record["requests"])
    claude = (1512 * 4 + 398 * 20) / 1e6
    lambda_ = (6800 + 24.5) / 1000 * 1 * 0.0000166667 + 0.2 / 1e6
    assert page["claude_usd"] == pytest.approx(claude) and page["lambda_usd"] == pytest.approx(lambda_)
    assert record["monthly_usd"] == round((claude + lambda_) * 3_000_000)
    assert record["production"] == {"version": "1.4.0", "measured": True, "monthly_usd": 3, "cost_json_build": "64"}
    assert record["delta_usd"] == record["monthly_usd"] - 3 and record["result"] == "above"
    assert cost_check.slack_line(record) == (f"Cost check: +USD {record['delta_usd']:,} a month at the expected volume. "
                                             "Above the USD 100 limit, so a cost approver decides.")


def test_the_change_s_five_lines(production):
    record = run("1.7.0", logs_170())
    lines = cost_check.change_lines(record, state("1.7.0"))
    assert lines[0] == (f"Cost check (staging, 50 page views): +USD {record['delta_usd']:,} a month at the expected "
                        "volume. Limit: USD 100 a month. Cost approvers decide.")
    assert lines[1] == ("Measured per page view in staging (50 page views): 1 Claude call, 1,512 tokens in and 398 out "
                        "on claude-opus-5-5; Lambda 6,825 ms billed at 1024 MB.")
    assert lines[2] == ("Prices: claude-opus-5-5 USD 4 in and USD 20 out per million tokens; Lambda USD 0.0000166667 "
                        "per GB-second (prices.yaml, read 30 October 2026).")
    assert lines[3] == "Expected volume (services.yaml): 3,000,000 page views and 60,000 new orders a month."
    assert lines[4] == "Production 1.4.0: USD 3 a month at the same volume."
    assert cost_check.change_lines(record, state("1.7.0", option=""))[0].endswith("Cost and change approvers decide.")


def test_a_volume_change_changes_the_projection_and_the_line(production):
    first = run("1.7.0", logs_170())
    SETTINGS["expected_month"]["page_views"] = 1_500_000
    try:
        second = run("1.7.0", logs_170())
    finally:
        SETTINGS["expected_month"]["page_views"] = 3_000_000
    assert second["monthly_usd"] == pytest.approx(first["monthly_usd"] / 2, abs=1)
    assert cost_check.slack_line(second) != cost_check.slack_line(first)


def test_one_seven_one_prices_its_per_order_call_per_new_order(production):
    record = run("1.7.1", logs_171())
    page, order = record["per_page_view"], record["per_new_order"]
    assert page["claude_calls"] == 0 and page["read_units"] == 1.5
    assert (order["claude_calls"], order["orders"], order["input_tokens"], order["output_tokens"]) == (1, 5, 210, 80)
    assert order["claude_usd"] == pytest.approx((1050 * 0.10 + 400 * 0.50) / 1e6 / 5)
    assert order["dynamodb_usd"] == pytest.approx(1.0 * 0.705 / 1e6)
    assert record["result"] == "within"
    line = cost_check.measured_line(record)
    assert line == ("Measured per page view in staging (50 page views): 0 Claude calls; DynamoDB 1.5 read units; "
                    "Lambda 41 ms billed at 1024 MB. Per new order: 1 Claude call for 5 orders, 210 tokens in and 80 out "
                    "per order on claude-haiku-5-5; DynamoDB 1 write units.")
    assert cost_check.slack_line(record).endswith("Within the USD 100 limit.")


def test_with_the_messages_stored_already_it_prices_the_last_seven_days_call(production):
    earlier = [claude_line("old-request", "1.7.1", "order", model="claude-haiku-5-5", tokens_in=1050, tokens_out=400),
               store_line("old-request", "1.7.1", 1.5, 5.0)]
    record = run("1.7.1", logs_171(first_call=False), earlier=earlier)
    assert record["result"] == "within" and record["driver"]["from_lookback"] is True
    assert "(from the last 7 days)" in cost_check.measured_line(record)
    nothing = run("1.7.1", logs_171(first_call=False))
    assert nothing["result"] == "no-measurement"
    assert nothing["reason"] == "no per-order call to price, in the sample or the last 7 days"


def test_two_calls_for_one_order_mean_the_store_isn_t_working(production):
    lines = logs_171()
    lines[3:3] = [claude_line(IDS[1], "1.7.1", "order", model="claude-haiku-5-5", tokens_in=1050, tokens_out=400)]
    record = run("1.7.1", lines)
    assert record["driver"]["used"] == "page_view" and "store isn't working" in record["driver"]["note"]
    assert record["per_new_order"]["total_usd"] == 0 and record["per_page_view"]["claude_calls"] == 0.04


@pytest.mark.parametrize("case, reason", [
    ("no reader", "the evidence reader role isn't available"),
    ("errors", "staging answered 6 of 50 page views with an error"),
    ("late logs", "the logs didn't arrive: 0 of 50 REPORT lines in 0 seconds"),
])
def test_without_a_measurement_a_cost_approver_decides(production, case, reason):
    reader = None if case == "no reader" else FakeReader(FakeLogs(logs_170(), arrive_after=1000))
    statuses = [502] * 6 + [200] * 44 if case == "errors" else None
    record = cost_check.check(state("1.7.0"), SETTINGS, reader, fetch=fetcher(statuses=statuses), logs_wait=0,
                              jcr_token="token", sleep=lambda s: None)
    assert record["result"] == "no-measurement" and record["reason"] == reason
    assert cost_check.slack_line(record) == f"Cost check: no measurement ({reason}). A cost approver decides."


def test_without_production_s_measurement_the_comparison_is_against_zero(monkeypatch):
    monkeypatch.setattr(publish, "request", lambda method, path, token, data=None, headers=None: (404, b""))
    record = run("1.4.0", [report(request_id, 52) for request_id in IDS], production_lambda=FakeLambda("1.3.0"))
    assert record["production"] == {"version": "1.3.0", "measured": False}
    assert record["delta_usd"] == record["monthly_usd"] == 3
    assert cost_check.slack_line(record) == ("Cost check: +USD 3 a month at the expected volume. Within the USD 100 "
                                             "limit (production has no cost measurement).")
    assert cost_check.change_lines(record, state("1.4.0"))[4] == \
        "Production 1.3.0: no cost measurement, so the comparison is against zero."


def test_the_run_state_routes_the_change_and_slack_gets_its_line(production, monkeypatch):
    import notify

    posted = []
    monkeypatch.setattr(notify, "post", lambda s: posted.append(s["lines"][-1]))
    monkeypatch.setattr(cost_check, "upload", lambda s, path: None)
    monkeypatch.setattr(cost_check, "send_event", lambda body: posted.append(body["monthly_usd"]))
    above, within = run("1.7.0", logs_170()), run("1.7.1", logs_171())
    run_state_ = state("1.7.0")
    cost_check.record_run(run_state_, above)
    assert run_state_["cost_review"] is True and len(run_state_["change_details"]) == 5
    assert (cost_check.run_state.REPORTS / "cost.json").exists()
    calm = state("1.7.1")
    cost_check.record_run(calm, within)
    assert "cost_review" not in calm and len(calm["change_work_notes"]) == 5
    assert posted[0].startswith("Cost check: +USD") and posted[1] == above["monthly_usd"]


def test_the_stage_comes_right_after_check_staging():
    run_state_ = {"stages": {"Staging": {}, "Check staging": {}, "Change": {}}}
    cost_check.add_stage(run_state_)
    cost_check.add_stage(run_state_)
    assert list(run_state_["stages"]) == ["Staging", "Check staging", "Cost check", "Change"]


def test_report_lines_in_text_and_json_and_the_function_s_own_lines():
    assert costs.report_of(report("abc-1", 7012)) == {"request_id": "abc-1", "billed_ms": 7012, "memory_mb": 1024}
    platform = json.dumps({"time": "2026-10-30T10:00:00Z", "type": "platform.report", "record": {
        "requestId": "abc-2", "metrics": {"billedDurationMs": 51, "memorySizeMB": 1024}}})
    assert costs.report_of(platform) == {"request_id": "abc-2", "billed_ms": 51, "memory_mb": 1024}
    assert costs.function_line(claude_line("abc-3", "1.7.0", "page_view"))["orders"] == ORDERS
    assert costs.function_line("[ERROR] KeyError: 'X'") is None


def test_amounts_are_whole_dollars_with_separators():
    assert costs.dollars(41234.5) == 41235 and costs.dollars(2.49) == 2
    assert costs.signed(41235) == "+USD 41,235" and costs.signed(-12) == "-USD 12" and costs.signed(0) == "+USD 0"
    assert [costs.price_text(v) for v in (4.0, 0.2, 0.1415, 0.0000166667, 20)] == ["4", "0.20", "0.1415",
                                                                                   "0.0000166667", "20"]


def test_the_event_carries_each_part_and_never_a_key_or_a_message(production):
    record = run("1.7.1", logs_171())
    body = cost_check.event_body(record, state("1.7.1"))
    assert body["event"] == "cost_check" and body["sample_size"] == 50 and body["limit_usd"] == 100
    assert body["per_new_order_claude_usd"] > 0 and body["per_page_view_lambda_usd"] > 0
    assert body["monthly_usd"] == record["monthly_usd"] and body["delta_usd"] == record["delta_usd"]
    assert "text" not in json.dumps(body) and "sk-ant" not in json.dumps(body)
