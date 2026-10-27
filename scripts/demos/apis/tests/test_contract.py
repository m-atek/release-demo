import io
import zipfile

import contract

REMOVED = {"id": "response-required-property-removed", "level": 3, "operation": "GET", "path": "/prices/{sku}",
           "text": "removed the required property `currency` from the response with the `200` status"}


def test_a_removed_field_is_named():
    assert contract.plain(REMOVED) == "field currency removed"
    assert contract.summary([REMOVED]) == "Contract check failed: field currency removed"


def test_other_breaking_changes_in_a_few_words():
    assert contract.plain({"id": "new-required-request-parameter",
                           "text": "added the new required `query` request parameter `region`"}) == \
        "new required parameter region"
    assert contract.plain({"id": "api-path-removed-without-deprecation", "operation": "GET", "path": "/products",
                           "text": "api path removed without deprecation"}) == "endpoint GET /products removed"
    assert contract.plain({"id": "something-new", "text": "the `x` changed"}) == "the x changed"


def test_summary_names_three_and_counts_the_rest():
    errors = [{"id": "response-required-property-removed", "text": f"removed the required property `f{n}`"}
              for n in range(5)]
    assert contract.summary(errors) == ("Contract check failed: field f0 removed; field f1 removed; "
                                        "field f2 removed and 2 more")


def test_the_contract_comes_out_of_the_package():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("openapi.yaml", "openapi: 3.0.3\n")
    assert contract.from_package(buffer.getvalue(), "openapi.yaml") == b"openapi: 3.0.3\n"
    assert contract.from_package(buffer.getvalue(), "missing.yaml") is None
