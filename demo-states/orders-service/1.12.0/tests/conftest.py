import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handler as app  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DEPLOYED_AT", "26 Oct 2026, 09:30 UTC")
    monkeypatch.setattr(app, "_deployed_at", None)


def request(path, method="GET", headers=None):
    event = {"rawPath": path, "headers": headers or {}, "requestContext": {"http": {"method": method, "path": path}}}
    return app.handler(event, None)


@pytest.fixture
def fake_brotli(monkeypatch):
    """The real module is built for Lambda's Linux and comes with the package, not with the
    toolchain the tests run in: a fake through sys.modules, as 1.6.0's and 1.10.0's tests do."""
    module = types.ModuleType("brotli")
    module.compress = lambda data: b"compressed:" + data
    monkeypatch.setitem(sys.modules, "brotli", module)
    return module
