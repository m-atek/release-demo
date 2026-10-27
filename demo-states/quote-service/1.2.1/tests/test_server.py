import datetime
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import server as app
from conftest import request


def test_health_reports_version_cloud_and_environment():
    status, _, body = request("/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["version"] == app.VERSION
    assert body["cloud"] == "azure"
    assert body["environment"] == "staging"


def test_unknown_cloud_shows_as_local(monkeypatch):
    monkeypatch.setenv("SERVICE_CLOUD", "elsewhere")
    assert request("/health")[2]["cloud"] == "local"


def test_page_shows_service_cloud_version_and_platform():
    status, content_type, body = request("/")
    assert status == 200
    assert content_type.startswith("text/html")
    assert "quote-service" in body
    assert app.VERSION in body
    assert "Azure Container Apps, westeurope" in body


def test_page_quotes_in_eur_and_gbp():
    body = request("/")[2]
    assert "Quotes in EUR and GBP" in body
    assert "103.20 GBP" in body
    assert "120.00 EUR" in body


def test_page_escapes_environment_name(monkeypatch):
    monkeypatch.setenv("SERVICE_ENVIRONMENT", "<staging>")
    assert "<staging>" not in request("/")[2]


def test_quote_in_gbp():
    status, _, body = request("/api/quote?amount=100&currency=gbp")
    assert status == 200
    assert body["currency"] == "GBP"
    assert body["total"] == 86.0


def test_gbp_rate_comes_from_the_setting(monkeypatch):
    monkeypatch.setenv("QUOTE_GBP_RATE", "0.9")
    assert request("/api/quote?amount=100&currency=GBP")[2]["total"] == 90.0


def test_without_the_setting_gbp_quotes_and_the_page_fail(monkeypatch):
    monkeypatch.delenv("QUOTE_GBP_RATE")
    status, _, body = request("/api/quote?amount=100&currency=GBP")
    assert status == 500
    assert body["error"] == "QUOTE_GBP_RATE is not set"
    assert request("/")[0] == 500
    assert request("/health")[0] == 200


def test_quote_in_eur_is_valid_for_30_days():
    body = app.quote(120, "EUR", today=datetime.date(2026, 10, 27))
    assert body["total"] == 120.0
    assert body["valid_until"] == "2026-11-26"


@pytest.mark.parametrize("target, message", [
    ("/api/quote?amount=100&currency=USD", "No quotes in USD"),
    ("/api/quote?amount=-5", "more than zero"),
    ("/api/quote?amount=lots", "must be a number"),
])
def test_quotes_that_cant_be_made(target, message):
    status, _, body = request(target)
    assert status == 400
    assert message in body["error"]


def test_unknown_path_and_method():
    assert request("/admin")[0] == 404
    assert request("/health", method="POST")[0] == 405


def test_serves_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json"
    finally:
        server.shutdown()
