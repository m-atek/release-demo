import sys
import types

import pytest

import app
from conftest import ROWS, get

CONNECT = app.connect  # the module's own, before each test's fake takes its place


def test_health_reports_version_environment_and_pod():
    status, body = get("/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["version"] == app.VERSION
    assert body["environment"] == "staging"
    assert body["pod"] == "catalog-7d9f8b6c5-abcde"


def test_health_never_asks_the_database(database):
    database.down = True
    assert get("/health")[0] == 200
    assert database.queries == []


def test_ready_while_the_database_answers():
    status, body = get("/ready")
    assert status == 200
    assert body["ready"] is True


def test_not_ready_while_the_database_doesnt_answer(database):
    database.down = True
    status, body = get("/ready")
    assert status == 503
    assert body["ready"] is False


@pytest.mark.parametrize("path", ["/", "/api/products", "/api/suppliers", "/api/products?supplier=wadden"])
def test_the_routes_that_need_the_database_answer_503_and_serve_no_data(database, path):
    database.down = True
    status, body = get(path)
    assert status == 503
    assert body == {"error": "the catalog database is unavailable"}


def test_each_query_has_a_connection_of_its_own(database):
    get("/api/products")
    get("/ready")
    assert database.connections == database.closed == 2


def test_a_failed_query_still_closes_its_connection(database, monkeypatch):
    monkeypatch.setattr(database, "run", lambda sql: (_ for _ in ()).throw(OSError("dropped")))
    assert get("/api/products")[0] == 503
    assert database.closed == 1


def test_the_page_asks_once(database):
    get("/")
    assert len(database.queries) == 1


def test_the_connection_comes_from_the_settings_with_a_time_limit_under_the_probes(monkeypatch):
    """The real connect(), against a stand-in for pg8000, which the toolchain doesn't have."""
    made = []
    native = types.SimpleNamespace(Connection=lambda **fields: made.append(fields) or "a connection")
    monkeypatch.setitem(sys.modules, "pg8000", types.SimpleNamespace(native=native))
    monkeypatch.setitem(sys.modules, "pg8000.native", native)
    assert CONNECT() == "a connection"
    assert made == [{"user": "catalog", "password": "test-value-not-a-secret", "host": "catalog-postgres", "port": 5432,
                     "database": "catalog", "timeout": 0.8}]
    assert app.DATABASE_TIMEOUT_SECONDS < 1


def test_no_setting_means_no_answer_not_a_crash(monkeypatch):
    monkeypatch.setattr(app, "connect", CONNECT)
    monkeypatch.delenv("CATALOG_POSTGRES_URL")
    assert get("/ready")[0] == 503


def test_the_catalog_has_twenty_four_products():
    status, body = get("/api/products")
    assert status == 200
    assert body["count"] == len(ROWS) == 24


def test_search_by_supplier():
    status, body = get("/api/products?supplier=wadden")
    assert status == 200
    assert body["count"] == 4
    assert {p["supplier"] for p in body["products"]} == {"Wadden Waterwerk"}


def test_search_by_name_and_supplier():
    body = get("/api/products?q=bottle&supplier=Wadden%20Waterwerk")[1]
    assert {p["name"] for p in body["products"]} == {"Water bottle", "Insulated bottle"}


def test_suppliers():
    assert len(get("/api/suppliers")[1]["suppliers"]) == 6


def test_page_shows_supplier_search():
    assert "Search by supplier: 6 suppliers" in get("/")[1]


def test_page_shows_service_version_and_pod():
    status, body = get("/")
    assert status == 200
    assert "catalog" in body
    assert app.VERSION in body
    assert "catalog-7d9f8b6c5-abcde" in body


def test_page_escapes_environment_name(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "<staging>")
    assert "<staging>" not in get("/")[1]


def test_search_by_name_ignores_case():
    status, body = get("/api/products?q=JACKET")
    assert status == 200
    assert body["count"] == 2
    assert {p["name"] for p in body["products"]} == {"Rain jacket", "Softshell jacket"}


def test_search_ignores_accents():
    assert app.folded("Crème") == "creme"


def test_every_product_has_a_supplier_and_price():
    for product in app.products():
        assert product["supplier"]
        assert product["price_eur"] > 0


def test_unknown_path_is_404():
    assert get("/admin")[0] == 404
