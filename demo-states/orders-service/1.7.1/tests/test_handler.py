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


def test_each_of_the_five_orders_gets_claudes_message(claude):
    body = request("/", request_id="req-1")["body"]
    orders = app.todays_orders()["orders"][:5]
    assert "<th>Delivery message</th>" in body
    for order in orders:
        assert f"Your order is on its way from {order['store']}." in body
    assert len(claude.calls) == 1


def test_each_order_is_written_once_and_the_next_page_view_reads_the_store(claude, store, capsys):
    first = request("/", request_id="req-1")["body"]
    second = request("/", request_id="req-2")["body"]
    assert len(claude.calls) == 1 and first.split("<tbody>")[1] == second.split("<tbody>")[1]
    lines = log_lines(capsys)
    ids = [o["id"] for o in app.todays_orders()["orders"][:5]]
    assert lines == [
        {"event": "claude", "request_id": "req-1", "version": app.VERSION, "per": "order", "orders": ids,
         "model": "claude-haiku-5-5", "input_tokens": 1480, "output_tokens": 410, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0},
        {"event": "store", "request_id": "req-1", "version": app.VERSION, "read_units": 1.5, "write_units": 5.0},
        {"event": "store", "request_id": "req-2", "version": app.VERSION, "read_units": 1.5, "write_units": 0.0}]
    assert "on its way" not in json.dumps(lines)
    table, item, condition = store.puts[0]
    assert table == "orders-service-messages-staging" and condition == "attribute_not_exists(order_id)"
    assert int(item["ttl"]["N"]) > 0


def test_only_the_orders_without_a_message_are_asked_for(claude, store):
    orders = app.todays_orders()["orders"][:5]
    store.items = {o["id"]: f"Stored message from {o['store']}." for o in orders[:3]}
    body = request("/")["body"]
    (call,) = claude.calls
    assert [o["order_id"] for o in json.loads(call["messages"][0]["content"])] == [o["id"] for o in orders[3:]]
    assert f"Stored message from {orders[0]['store']}." in body


def test_a_race_never_writes_twice(store):
    order = app.todays_orders()["orders"][0]
    store.items[order["id"]] = "first"
    assert app.store_message(order["id"], "second") == 0.0
    assert store.items[order["id"]] == "first"


def test_an_api_error_stores_nothing_and_asks_again_next_time(claude, store):
    claude.error = TimeoutError("no answer")
    request("/")
    request("/")
    assert store.puts == [] and len(claude.calls) == 2


def test_without_the_store_every_page_view_asks_and_says_why(claude, store, capsys):
    store.broken = True
    assert "on its way" in request("/")["body"]
    events = [line["event"] for line in log_lines(capsys)]
    assert events[0] == "store_error" and "claude" in events and events.count("store_error") == 6


def test_the_request_asks_for_the_schema_at_low_effort_on_haiku(claude):
    request("/")
    (call,) = claude.calls
    assert call["model"] == "claude-haiku-5-5" and call["max_tokens"] == 2000
    assert call["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": app.MESSAGE_SCHEMA}}
    assert "fallbacks" not in call and "betas" not in call  # Claude Haiku 5.5 has no server-side fallback
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
    assert log_lines(capsys)[0] == {"event": "claude_error", "request_id": "req-3", "version": app.VERSION,
                                    "error": "TimeoutError"}


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
    line = log_lines(capsys)[0]
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


def test_the_store_client_is_made_once(monkeypatch):
    import types

    made = []
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: made.append(name) or "dynamodb"))
    monkeypatch.setattr(app, "_dynamodb", None)
    assert app.dynamodb() == "dynamodb" and app.dynamodb() == "dynamodb" and made == ["dynamodb"]
    monkeypatch.setenv("MESSAGES_TABLE", "orders-service-messages-production")
    assert app.messages_table() == "orders-service-messages-production"
