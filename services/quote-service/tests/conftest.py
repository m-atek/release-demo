import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as app  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SERVICE_CLOUD", "azure")
    monkeypatch.setenv("SERVICE_ENVIRONMENT", "staging")
    monkeypatch.setenv("SERVICE_REGION", "westeurope")
    monkeypatch.setenv("QUOTE_GBP_RATE", "0.86")


def request(target, method="GET"):
    """(status, content type, body), parsed when JSON."""
    status, content_type, body = app.respond(method, target)
    return status, content_type, json.loads(body) if content_type == "application/json" else body
