"""The handler keeps to its own contract, openapi.yaml. The release pipeline's
contract check then compares that file with production's."""

import json
from pathlib import Path

import yaml

import handler as app
from conftest import request

SPEC = yaml.safe_load((Path(app.__file__).parent / "openapi.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]


def fields(schema_name):
    schema = SCHEMAS[schema_name]
    return set(schema["required"]), set(schema["properties"])


def assert_matches(response, schema_name):
    required, allowed = fields(schema_name)
    keys = set(json.loads(response["body"]))
    assert required <= keys, f"missing {required - keys}"
    assert keys <= allowed, f"not in the contract: {keys - allowed}"


def test_contract_version_is_the_service_version():
    assert SPEC["info"]["version"] == app.VERSION


def test_every_path_answered_is_in_the_contract():
    assert set(SPEC["paths"]) == {"/health", "/products", "/prices/{sku}"}


def test_answers_match_their_schemas():
    assert_matches(request("/health"), "Health")
    assert_matches(request("/products"), "Products")
    for store in app.STORES:
        assert_matches(request("/prices/RS-100", store), "Price")
    assert_matches(request("/prices/RS-100"), "Error")
    assert_matches(request("/prices/ZZ-999", "NL-UTR-01"), "Error")
