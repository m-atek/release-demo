import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handler as app  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DEPLOYED_AT", "26 Oct 2026, 09:30 UTC")
    monkeypatch.setattr(app, "_deployed_at", None)


class FakeClaude:
    """The SDK client's beta.messages.create, answering with each order's
    message, or raising. No network: the tests never reach the API."""

    def __init__(self, write=None, error=None, stop_reason="end_turn", model="claude-opus-5-5"):
        self.write = write or (lambda order: f"Your order is on its way from {order['store']}.")
        self.error, self.stop_reason, self.model, self.calls = error, stop_reason, model, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.calls.append(request)
        if self.error:
            raise self.error
        orders = json.loads(request["messages"][0]["content"])
        text = json.dumps({"messages": [{"order_id": o["order_id"], "text": self.write(o)} for o in orders]})
        usage = SimpleNamespace(input_tokens=1480, output_tokens=410, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, iterations=None)
        return SimpleNamespace(model=self.model, stop_reason=self.stop_reason, usage=usage,
                               content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])


@pytest.fixture(autouse=True)
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(app, "_claude", fake)
    return fake


def request(path, method="GET", request_id=None):
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "path": path}}}
    context = SimpleNamespace(aws_request_id=request_id) if request_id else None
    return app.handler(event, context)


def log_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
