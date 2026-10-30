import json
import sys
import types

import pytest

import handler as app
from conftest import DatabaseError, FakeConnection, InterfaceError, request


def test_health_reports_version_and_environment():
    response = request("/health")
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["status"] == "ok"
    assert body["version"] == app.VERSION
    assert body["environment"] == "staging"


def test_health_answers_with_the_database_unreachable(monkeypatch):
    monkeypatch.setitem(app._database, "url", None)
    monkeypatch.setitem(app._database, "connection", None)
    assert request("/health")["statusCode"] == 200


def test_page_shows_service_version_and_deploy_time(database):
    response = request("/")
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert "orders-service" in response["body"]
    assert app.VERSION in response["body"]
    assert "26 Oct 2026, 09:30 UTC" in response["body"]


def test_page_shows_delivery_estimate(database):
    assert "Delivery estimate: 2 days" in request("/")["body"]


def test_page_lists_the_orders_with_their_delivery(database):
    body = request("/")["body"]
    assert "Open orders (3)" in body
    assert "<td>Groningen Vismarkt</td><td>2</td><td>&euro; 99.49</td><td>received</td><td>3 days</td>" in body
    assert database.queries == [app.PAGE_SQL]


def test_page_escapes_environment_name(monkeypatch, database):
    monkeypatch.setenv("ENVIRONMENT", "<staging>")
    assert "<staging>" not in request("/")["body"]


def test_orders_api_returns_the_orders(database):
    body = json.loads(request("/api/orders")["body"])
    assert [order["id"] for order in body["orders"]] == ["26-1001", "26-1002", "26-1006"]
    assert body["orders"][0] == {"id": "26-1001", "store": "Utrecht Centraal", "items": ["Running shoes", "Water bottle"],
                                 "total_eur": 102.9, "status": "packed", "delivery_estimate": 2}
    assert database.queries == [app.API_SQL]


def test_a_database_error_is_logged_in_one_line_and_raised(monkeypatch, capsys):
    error = DatabaseError({"S": "ERROR", "C": "42703", "M": 'column "x" does not exist'})
    monkeypatch.setitem(app._database, "url", "postgresql://db.example/orders")
    monkeypatch.setitem(app._database, "connection", FakeConnection(error=error))
    with pytest.raises(DatabaseError):
        request("/api/orders")
    assert capsys.readouterr().out == f'{app.VERSION} database error: column "x" does not exist\n'
    assert app._database["connection"] is None


def test_a_dropped_connection_is_opened_again_once(monkeypatch):
    fresh = FakeConnection()
    monkeypatch.setitem(app._database, "url", "postgresql://db.example/orders")
    monkeypatch.setitem(app._database, "connection", FakeConnection(error=InterfaceError("network error")))
    monkeypatch.setattr(app, "connect", lambda url: fresh)
    assert request("/api/orders")["statusCode"] == 200
    assert fresh.queries == [app.API_SQL]


def test_without_a_connection_string_the_orders_fail(monkeypatch, capsys):
    monkeypatch.setitem(app._database, "url", None)
    monkeypatch.setitem(app._database, "connection", None)
    with pytest.raises(RuntimeError):
        request("/")
    assert "database error: no database connection string" in capsys.readouterr().out


def test_connect_opens_a_tls_connection_from_the_url(monkeypatch):
    made = {}
    native = types.SimpleNamespace(Connection=lambda **kwargs: made.update(kwargs) or "connection")
    monkeypatch.setitem(sys.modules, "pg8000", types.SimpleNamespace(native=native))
    monkeypatch.setitem(sys.modules, "pg8000.native", native)
    assert app.connect("postgresql://orders_app:p%40ss@ep-1.example.neon.tech/orders?sslmode=require") == "connection"
    assert made["user"] == "orders_app" and made["password"] == "p@ss"
    assert made["host"] == "ep-1.example.neon.tech" and made["port"] == 5432 and made["database"] == "orders"
    assert made["ssl_context"].verify_mode.name == "CERT_REQUIRED"


def test_the_connection_string_comes_from_ssm_in_lambda(monkeypatch):
    calls = []

    class SSM:
        def get_parameter(self, Name, WithDecryption):
            calls.append((Name, WithDecryption))
            return {"Parameter": {"Value": "postgresql://db.example/orders"}}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: SSM()))
    monkeypatch.setenv("DATABASE_URL_PARAMETER", "orders-service/staging/database-url")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "orders-service-staging")
    assert app.database_url() == "postgresql://db.example/orders"
    assert calls == [("/release-demo/orders-service/staging/database-url", True)]
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME")
    assert app.database_url() is None


def test_a_missing_parameter_leaves_the_database_unset(monkeypatch, capsys):
    def broken(name):
        raise PermissionError("denied")

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=broken))
    monkeypatch.setenv("DATABASE_URL_PARAMETER", "orders-service/staging/database-url")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "orders-service-staging")
    assert app.database_url() is None
    assert capsys.readouterr().out == f"{app.VERSION} database settings: PermissionError\n"


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
