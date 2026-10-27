import json

import handler as app
from conftest import request


def body(response):
    return json.loads(response["body"])


def test_health_reports_version_and_environment():
    response = request("/health")
    assert response["statusCode"] == 200
    assert body(response)["status"] == "ok"
    assert body(response)["version"] == app.VERSION
    assert body(response)["environment"] == "staging"


def test_every_answer_names_its_version():
    for response in (request("/health"), request("/prices/RS-100", "NL-UTR-01"), request("/nowhere")):
        assert response["headers"]["x-pricing-api-version"] == app.VERSION


def test_products_lists_every_sku():
    products = body(request("/products"))["products"]
    assert len(products) == len(app.PRODUCTS)
    assert {"sku": "RS-100", "name": "Running shoes"} in products


def test_dutch_store_gets_the_national_price():
    response = request("/prices/RS-100", "NL-UTR-01")
    assert response["statusCode"] == 200
    assert body(response) == {"sku": "RS-100", "name": "Running shoes", "store": "NL-UTR-01",
                              "price": 89.95, "currency": "EUR", "price_list": "national"}


def test_belgian_store_gets_the_regional_price():
    price = body(request("/prices/RS-100", "BE-ANT-01"))
    assert price["price"] == 92.95
    assert price["price_list"] == "regional"


def test_belgian_store_falls_back_to_the_national_price():
    price = body(request("/prices/HL-320", "BE-BRU-02"))
    assert price["price"] == 34.95
    assert price["price_list"] == "national"


def test_every_product_has_a_price_in_every_store():
    for store in app.STORES:
        for sku in app.PRODUCTS:
            assert request(f"/prices/{sku}", store)["statusCode"] == 200, (sku, store)


def test_store_is_required_and_must_exist():
    assert request("/prices/RS-100")["statusCode"] == 400
    assert request("/prices/RS-100", "NL-XXX-99")["statusCode"] == 404


def test_unknown_product_path_and_method():
    assert request("/prices/ZZ-999", "NL-UTR-01")["statusCode"] == 404
    assert request("/admin")["statusCode"] == 404
    assert request("/products", method="POST")["statusCode"] == 405


def test_commit_comes_from_build_info(tmp_path):
    (tmp_path / "VERSION").write_text("9.9.9\n")
    assert app.read_package(tmp_path) == ("9.9.9", "local")
    (tmp_path / "build_info.json").write_text('{"commit": "abc1234def"}')
    assert app.read_package(tmp_path) == ("9.9.9", "abc1234def")
