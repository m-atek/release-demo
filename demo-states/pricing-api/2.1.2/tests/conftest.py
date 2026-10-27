import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handler as app  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")


def request(path, store=None, method="GET"):
    """An API Gateway HTTP API event (payload format 2.0), as the live alias gets it."""
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "path": path}}}
    if store is not None:
        event["queryStringParameters"] = {"store": store}
    return app.handler(event, None)
