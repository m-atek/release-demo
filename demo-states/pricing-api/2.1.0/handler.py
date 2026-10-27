"""pricing-api: shelf prices for every store, the API that demo 4 releases.

An AWS Lambda function behind an API Gateway HTTP API, which calls its alias
live. Its contract is openapi.yaml, next to this file: the release pipeline
compares it with production's before anything ships. Its fictional consumers
are orders-service and the store app. Standard library only.

    GET /health                          status and version, for the pipeline's checks
    GET /products                        every product with its SKU
    GET /prices/{sku}?store=NL-UTR-01    the shelf price of one product in one store

Every answer names the version that gave it in the header
x-pricing-api-version. Nothing here catches an unexpected error: it raises,
so Lambda counts it, and the canary's alarm watches exactly that count.
"""

import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = "pricing-api"

# SKU: name and national shelf price in euros, VAT included.
PRODUCTS = {
    "RS-100": ("Running shoes", 89.95),
    "TS-110": ("Trail shoes", 109.00),
    "RJ-200": ("Rain jacket", 74.50),
    "RT-210": ("Running tights", 39.95),
    "TR-220": ("Training shirt", 24.99),
    "SO-230": ("Sports socks, 3 pairs", 14.99),
    "CP-240": ("Running cap", 17.50),
    "DB-300": ("Day backpack", 49.00),
    "WB-310": ("Water bottle", 12.95),
    "HL-320": ("Headlamp", 34.95),
    "YM-400": ("Yoga mat", 29.99),
    "FR-410": ("Foam roller", 22.50),
}
STORES = {
    "NL-UTR-01": "Utrecht Centraal",
    "NL-AMS-02": "Amsterdam Zuid",
    "NL-RTM-03": "Rotterdam Blaak",
    "NL-EHV-04": "Eindhoven Strijp",
    "BE-ANT-01": "Antwerpen Meir",
    "BE-BRU-02": "Brussel Nieuwstraat",
    "BE-GNT-03": "Gent Veldstraat",
}
PRICE_PATH = re.compile(r"/prices/([A-Z]{2}-[0-9]{3})")
# Regions with their own price list (ORD-52): Belgian stores sell these
# products at Belgian prices.
REGIONAL_PRICES = {
    "BE": {"RS-100": 92.95, "RJ-200": 76.50, "DB-300": 49.95},
}


def read_package(directory):
    """The version, and the commit the build step wrote, or "local" before a build."""
    version = (directory / "VERSION").read_text().strip()
    info = directory / "build_info.json"
    commit = json.loads(info.read_text()).get("commit", "unknown") if info.exists() else "local"
    return version, commit


# Read once at cold start: a published Lambda version never changes.
VERSION, COMMIT = read_package(HERE)


def health():
    return {
        "status": "ok",
        "service": SERVICE,
        "environment": os.environ.get("ENVIRONMENT", "local"),
        "version": VERSION,
        "commit": COMMIT,
    }


def products():
    return {"products": [{"sku": sku, "name": name} for sku, (name, _) in PRODUCTS.items()]}


def shelf_price(sku, store):
    """A store in a region with its own price list gets the regional price."""
    region = store.split("-")[0]
    if region in REGIONAL_PRICES:
        return REGIONAL_PRICES[region][sku], "regional"
    return PRODUCTS[sku][1], "national"


def price(sku, store):
    if not store:
        return _json(400, {"error": "the query parameter store is required"})
    if store not in STORES:
        return _json(404, {"error": f"unknown store {store}"})
    if sku not in PRODUCTS:
        return _json(404, {"error": f"unknown product {sku}"})
    amount, price_list = shelf_price(sku, store)
    return _json(200, {
        "sku": sku,
        "name": PRODUCTS[sku][0],
        "store": store,
        "price": amount,
        "price_list": price_list,
    })


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    query = event.get("queryStringParameters") or {}

    if method != "GET":
        return _json(405, {"error": "method not allowed"})
    if path == "/health":
        return _json(200, health())
    if path == "/products":
        return _json(200, products())
    match = PRICE_PATH.fullmatch(path)
    if match:
        return price(match.group(1), query.get("store"))
    return _json(404, {"error": "not found"})


def _json(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store",
                    "x-pricing-api-version": VERSION},
        "body": json.dumps(body),
    }
