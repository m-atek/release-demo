import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

import run_state  # noqa: E402


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


ORDERS = ["261030-001", "261030-002", "261030-003", "261030-004", "261030-005"]


def report(request_id, billed_ms, memory=1024):
    return (f"REPORT RequestId: {request_id}\tDuration: {billed_ms - 0.4:.2f} ms\tBilled Duration: {billed_ms} ms\t"
            f"Memory Size: {memory} MB\tMax Memory Used: 141 MB\n")


def claude_line(request_id, version, per, orders=ORDERS, model="claude-opus-5-5", tokens_in=1512, tokens_out=398):
    return json.dumps({"event": "claude", "request_id": request_id, "version": version, "per": per, "orders": orders,
                       "model": model, "input_tokens": tokens_in, "output_tokens": tokens_out,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}) + "\n"


def store_line(request_id, version, read_units, write_units):
    return json.dumps({"event": "store", "request_id": request_id, "version": version, "read_units": read_units,
                       "write_units": write_units}) + "\n"


class FakeLogs:
    """FilterLogEvents on staging's log group: the events the test gives, all at once or arriving late."""

    def __init__(self, messages, earlier=(), arrive_after=0):
        self.messages, self.earlier, self.arrive_after, self.calls = list(messages), list(earlier), arrive_after, []

    def get_paginator(self, name):
        logs = self

        class Paginator:
            def paginate(self, **kwargs):
                logs.calls.append(kwargs)
                if "filterPattern" in kwargs:
                    wanted = "claude" if '"claude"' in kwargs["filterPattern"] else "store"
                    found = [m for m in logs.earlier if f'"event": "{wanted}"' in m]
                    yield {"events": [{"timestamp": 0, "message": m} for m in found]}
                    return
                ready = len([c for c in logs.calls if "filterPattern" not in c]) > logs.arrive_after
                yield {"events": [{"timestamp": 0, "message": m} for m in logs.messages] if ready else []}
        return Paginator()


class FakeLambda:
    def __init__(self, description="1.4.0", code="aGVsbG8="):
        self.description, self.code = description, code

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": "64"}

    def get_function_configuration(self, FunctionName, Qualifier):
        return {"Description": self.description, "CodeSha256": self.code}


class FakeReader:
    def __init__(self, logs, lam=None):
        self.logs, self.lam = logs, lam or FakeLambda()

    def client(self, name):
        return self.logs if name == "logs" else self.lam
