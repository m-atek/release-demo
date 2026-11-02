import app
from conftest import get


def test_health_reports_version_environment_and_pod():
    status, body = get("/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["version"] == app.VERSION
    assert body["environment"] == "staging"
    assert body["pod"] == "catalog-7d9f8b6c5-abcde"


def test_ready_once_the_catalog_loads():
    status, body = get("/ready")
    assert status == 200
    assert body["ready"] is True


def test_not_ready_when_the_catalog_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_DB_URL", f"file://{tmp_path / 'missing.json'}")
    status, body = get("/ready")
    assert status == 503
    assert body["ready"] is False


def test_not_ready_without_the_catalog_database_setting(monkeypatch):
    monkeypatch.delenv("CATALOG_DB_URL")
    status, body = get("/ready")
    assert status == 503
    assert body["reason"] == "CATALOG_DB_URL is not set"
    # Alive all the same: the liveness probe mustn't restart it.
    assert get("/health")[0] == 200
    assert get("/api/products")[0] == 503


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
