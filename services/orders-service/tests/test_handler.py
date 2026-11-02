import datetime
import json

import handler as app
from conftest import request


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


def test_store_counts_busiest_first():
    stores = ["Den Haag Spui", "Rotterdam Blaak", "Amsterdam Zuid", "Rotterdam Blaak", "Amsterdam Zuid", "Rotterdam Blaak"]
    counts = app.store_counts([{"store": store} for store in stores])
    assert counts == [("Rotterdam Blaak", 3), ("Amsterdam Zuid", 2), ("Den Haag Spui", 1)]


def test_page_shows_order_count_per_store_busiest_first():
    body = request("/")["body"].split("Orders per store today", 1)[1]
    orders = app.todays_orders()["orders"]
    counts = app.store_counts(orders)
    assert sum(count for _, count in counts) == len(orders)
    positions = [body.index(f"<tr><td>{store}</td><td>{count}</td></tr>") for store, count in counts]
    assert positions == sorted(positions)
    assert [count for _, count in counts] == sorted((count for _, count in counts), reverse=True)


def test_orders_api_has_no_store_counts():
    body = json.loads(request("/api/orders")["body"])
    assert set(body) == {"date", "orders"}


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
