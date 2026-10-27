import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("POD_NAME", "catalog-7d9f8b6c5-abcde")
    monkeypatch.setenv("CATALOG_DB_URL", f"file://{Path(app.HERE) / 'data' / 'catalog.json'}")


def get(path):
    """(status, body) for a GET, the body parsed when it is JSON."""
    status, content_type, body = app.respond(path)
    return status, json.loads(body) if content_type == "application/json" else body
