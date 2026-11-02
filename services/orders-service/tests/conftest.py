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
    monkeypatch.setattr(app, "_flags", {"client": None, "token": None, "values": {}, "next_poll": 0.0})


@pytest.fixture
def flags(monkeypatch):
    """The flags as AWS AppConfig hands them out, set by each test."""
    values = {}
    monkeypatch.setattr(app, "_poll_flags", lambda: app._flags.update(values=dict(values)))
    return values


def request(path, method="GET", query=None):
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "path": path}}}
    if query:
        event["queryStringParameters"] = query
    return app.handler(event, None)
