import datetime
import json
import sys

import pytest

import handler as app
from conftest import log_lines, request


def test_health_reports_version_and_environment():
    response = request("/health")
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["status"] == "ok"
    assert body["version"] == app.VERSION
    assert body["environment"] == "staging"


def test_page_shows_service_version_and_deploy_time():
    response = request("/")
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert "orders-service" in response["body"]
    assert app.VERSION in response["body"]
    assert "26 Oct 2026, 09:30 UTC" in response["body"]


def test_page_shows_delivery_estimate():
    assert "Delivery estimate: 2 days" in request("/")["body"]


def test_page_escapes_environment_name(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "<staging>")
    assert "<staging>" not in request("/")["body"]


def test_orders_are_the_same_all_day():
    day = datetime.date(2026, 10, 26)
    first, again = app.todays_orders(day), app.todays_orders(day)
    assert first == again
    assert app.todays_orders(day) != app.todays_orders(day + datetime.timedelta(days=1))


def test_orders_api_returns_todays_orders():
    body = json.loads(request("/api/orders")["body"])
    assert body["date"] == datetime.date.today().isoformat()
    assert 8 <= len(body["orders"]) <= 14
    assert all(order["total_eur"] > 0 for order in body["orders"])


def test_unknown_path_and_method():
    assert request("/admin")["statusCode"] == 404
    assert request("/", method="POST")["statusCode"] == 405


def test_commit_comes_from_build_info(tmp_path):
    (tmp_path / "VERSION").write_text("9.9.9\n")
    assert app.read_package(tmp_path) == ("9.9.9", "local")
    (tmp_path / "build_info.json").write_text('{"commit": "abc1234def"}')
    assert app.read_package(tmp_path) == ("9.9.9", "abc1234def")


def test_deploy_time_unknown_outside_lambda(monkeypatch):
    monkeypatch.delenv("DEPLOYED_AT")
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert app.deployed_at() == "unknown"


def test_each_of_the_five_orders_gets_claudes_message(claude, capsys):
    body = request("/", request_id="req-1")["body"]
    orders = app.todays_orders()["orders"][:5]
    assert "<th>Delivery message</th>" in body
    for order in orders:
        assert f"Your order is on its way from {order['store']}." in body
    assert len(claude.calls) == 1


def test_one_call_per_page_view_logged_without_its_text(claude, capsys):
    request("/", request_id="req-1")
    request("/", request_id="req-2")
    lines = log_lines(capsys)
    assert [line["request_id"] for line in lines] == ["req-1", "req-2"]
    first = lines[0]
    assert first == {"event": "claude", "request_id": "req-1", "version": app.VERSION, "per": "page_view",
                     "orders": [o["id"] for o in app.todays_orders()["orders"][:5]], "model": "claude-opus-5-5",
                     "input_tokens": 1480, "output_tokens": 410, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0}
    assert "on its way" not in json.dumps(lines)


def test_the_request_asks_for_the_schema_at_low_effort_with_the_fallback(claude):
    request("/")
    (call,) = claude.calls
    assert call["model"] == "claude-opus-5-5" and call["max_tokens"] == 2000
    assert call["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": app.MESSAGE_SCHEMA}}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    sent = json.loads(call["messages"][0]["content"])
    assert set(sent[0]) == {"order_id", "store", "items", "status", "delivery_estimate_days"}
    assert "temperature" not in call


@pytest.mark.parametrize("write", [lambda order: "x" * 121 + order["store"], lambda order: "Your order is on its way."])
def test_a_message_too_long_or_without_its_store_gives_the_template(claude, write):
    claude.write = write
    body = request("/")["body"]
    order = app.todays_orders()["orders"][0]
    assert f"Ships from {order['store']}. Delivery estimate: 2 days." in body


def test_any_error_gives_the_template_and_the_page_still_answers(claude, capsys):
    claude.error = TimeoutError("no answer in 20 seconds")
    response = request("/", request_id="req-3")
    assert response["statusCode"] == 200
    assert "Ships from" in response["body"]
    assert log_lines(capsys) == [{"event": "claude_error", "request_id": "req-3", "version": app.VERSION,
                                  "error": "TimeoutError"}]


def test_a_refusal_is_read_from_stop_reason_before_any_content(claude):
    claude.stop_reason = "refusal"
    assert "Ships from" in request("/")["body"]


def test_health_and_the_api_make_no_call(claude):
    request("/health")
    request("/api/orders")
    assert claude.calls == []


def test_the_key_is_read_from_ssm_once_per_cold_start(monkeypatch):
    import types

    reads, made = [], []
    boto3 = types.SimpleNamespace(client=lambda name: types.SimpleNamespace(
        get_parameter=lambda Name, WithDecryption: reads.append(Name) or {"Parameter": {"Value": "sk-ant-test"}}))
    anthropic = types.SimpleNamespace(Anthropic=lambda **kwargs: made.append(kwargs) or "client")
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)
    monkeypatch.setattr(app, "_claude", None)
    monkeypatch.setenv("ANTHROPIC_KEY_PARAMETER", "/release-demo/anthropic/api-key")
    assert app.claude() == "client" and app.claude() == "client"
    assert reads == ["/release-demo/anthropic/api-key"]
    assert made == [{"api_key": "sk-ant-test", "timeout": 20.0, "max_retries": 0}]


def test_a_fallback_logs_every_attempt_by_its_model(claude, capsys, monkeypatch):
    from types import SimpleNamespace

    attempt = SimpleNamespace(model="claude-opus-5-5", input_tokens=1480, output_tokens=0, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0)
    served = SimpleNamespace(model="claude-opus-5", input_tokens=1480, output_tokens=420, cache_read_input_tokens=0,
                             cache_creation_input_tokens=0)
    create = claude.create

    def with_fallback(**request):
        response = create(**request)
        response.model, response.usage.iterations = "claude-opus-5", [attempt, served]
        return response
    claude.beta.messages.create = with_fallback
    request("/", request_id="req-4")
    (line,) = log_lines(capsys)
    assert line["model"] == "claude-opus-5"
    assert [(i["model"], i["output_tokens"]) for i in line["iterations"]] == [("claude-opus-5-5", 0), ("claude-opus-5", 420)]


def test_an_answer_that_isnt_the_schemas_json_gives_the_template(claude, monkeypatch):
    from types import SimpleNamespace

    create = claude.create

    def prose(**request):
        response = create(**request)
        response.content = [SimpleNamespace(type="text", text="Here are your messages.")]
        return response
    claude.beta.messages.create = prose
    assert "Ships from" in request("/")["body"]
