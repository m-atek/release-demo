import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("POD_NAME", "forecast-7d9f8b6c5-abcde")


def as_json(reply):
    status, body = reply
    return status, json.loads(json.dumps(body))
