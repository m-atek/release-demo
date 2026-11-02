import base64
import datetime
import json
import sys
from pathlib import Path

import pytest

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


def test_orders_api_answers_in_brotli_when_the_request_accepts_it(fake_brotli):
    response = request("/api/orders", headers={"accept-encoding": "gzip, deflate, br"})
    assert response["statusCode"] == 200
    assert response["headers"]["content-encoding"] == "br"
    assert response["headers"]["content-type"] == "application/json"
    assert response["isBase64Encoded"] is True
    compressed = base64.b64decode(response["body"])
    assert compressed.startswith(b"compressed:")
    assert json.loads(compressed.removeprefix(b"compressed:")) == app.todays_orders()


def test_a_header_name_may_come_in_any_case(fake_brotli):
    assert request("/api/orders", headers={"Accept-Encoding": "BR"})["headers"]["content-encoding"] == "br"


@pytest.mark.parametrize("headers", [{}, {"accept-encoding": "gzip, deflate"}, {"accept-encoding": "identity"},
                                     {"accept-encoding": "br;q=0"}, {"accept-encoding": "br; q=0.000, gzip"}])
def test_a_request_that_doesnt_accept_br_never_loads_the_package(headers, monkeypatch):
    monkeypatch.setitem(sys.modules, "brotli", None)  # importing it would raise
    response = request("/api/orders", headers=headers)
    assert "content-encoding" not in response["headers"] and "isBase64Encoded" not in response
    assert json.loads(response["body"]) == app.todays_orders()


def test_a_quality_above_0_accepts_br(fake_brotli):
    assert request("/api/orders", headers={"accept-encoding": "br;q=0.5"})["isBase64Encoded"] is True


@pytest.mark.parametrize("path", ["/", "/health"])
def test_only_the_orders_api_is_compressed(path, monkeypatch):
    monkeypatch.setitem(sys.modules, "brotli", None)
    assert "content-encoding" not in request(path, headers={"accept-encoding": "br"})["headers"]


def test_it_never_decodes():
    # The CVE is in the decoder, so nothing public reaches it.
    assert "decompress" not in Path(app.__file__).read_text().lower()


def test_the_page_has_no_demo_tag():
    assert "Demo system" not in request("/")["body"]
