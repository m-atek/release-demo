import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handler as app  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DEPLOYED_AT", "26 Oct 2026, 09:30 UTC")
    monkeypatch.setattr(app, "_deployed_at", None)


def request(path, method="GET", query=None):
    path = path.split("?", 1)[0]
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "path": path}}}
    if query:
        event["queryStringParameters"] = query
    return app.handler(event, None)
