import decimal
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

ROW = re.compile(r"\('(P-\d+)', '(.+?)', '(.+?)', ([\d.]+)\)")
ROWS = [(id_, name, supplier, decimal.Decimal(price))
        for id_, name, supplier, price in ROW.findall((app.HERE / "database" / "seed.sql").read_text())]


class FakeDatabase:
    """Stands in for Postgres, which the toolchain doesn't have: each connection answers from
    the seed, and while it is down no connection opens, as pg8000 refuses or times out."""

    def __init__(self):
        self.down, self.connections, self.closed, self.queries = False, 0, 0, []

    def connect(self):
        if self.down:
            raise OSError("connection refused")
        self.connections += 1
        return self

    def run(self, sql):
        self.queries.append(sql)
        return [[len(ROWS)]] if "count(" in sql else [list(row) for row in ROWS]

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("POD_NAME", "catalog-7d9f8b6c5-abcde")
    monkeypatch.setenv("CATALOG_POSTGRES_URL", "postgresql://catalog@catalog-postgres:5432/catalog")
    monkeypatch.setenv("CATALOG_POSTGRES_PASSWORD", "test-value-not-a-secret")


@pytest.fixture(autouse=True)
def database(monkeypatch):
    fake = FakeDatabase()
    monkeypatch.setattr(app, "connect", fake.connect)
    return fake


def get(path):
    """(status, body) for a GET, the body parsed when it is JSON."""
    status, content_type, body = app.respond(path)
    return status, json.loads(body) if content_type == "application/json" else body
