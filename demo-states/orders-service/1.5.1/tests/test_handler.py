import datetime
import json
import time

import handler as app
from conftest import request

# The page must render fast for every region: RENDERS_PER_REGION renders of
# each, in under TIME_LIMIT_SECONDS. scripts/demos/flaky/calibrate.py times
# this test 200 times with coverage, as the pipeline runs it, and sets the
# limit near the 90th percentile.
RENDERS_PER_REGION = 200
# Measured: p50 0.098 s, p90 0.102 s, p99 0.107 s in 200 runs on a Mac, 30 Oct 2026.
TIME_LIMIT_SECONDS = 0.102


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
    assert "Delivery estimate: 2 days in Utrecht" in request("/")["body"]


def test_delivery_estimate_per_region():
    for store, region in app.STORE_REGIONS.items():
        days = app.delivery_estimate(region)
        assert days >= 1, store
        assert f"Delivery estimate: {days} days in {region}" in app.page(region)
    assert request("/?region=Atlantis", query={"region": "Atlantis"})["statusCode"] == 404


def render_every_region():
    started = time.perf_counter()
    for region in app.regions():
        for _ in range(RENDERS_PER_REGION):
            app.page(region)
    return time.perf_counter() - started


def test_delivery_estimate_timeout(record_property):
    elapsed = render_every_region()
    record_property("elapsed_seconds", f"{elapsed:.4f}")
    assert elapsed < TIME_LIMIT_SECONDS, (
        f"{RENDERS_PER_REGION} renders per region took {elapsed:.3f}s, over the {TIME_LIMIT_SECONDS}s limit")


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
