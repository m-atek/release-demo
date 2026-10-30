import decimal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handler as app  # noqa: E402

ROWS = [
    ("26-1001", "Utrecht Centraal", ["Running shoes", "Water bottle"], decimal.Decimal("102.90"), "packed", 2),
    ("26-1002", "Amsterdam Zuid", ["Rain jacket"], decimal.Decimal("74.50"), "picking", 2),
    ("26-1006", "Groningen Vismarkt", ["Rain jacket", "Training shirt"], decimal.Decimal("99.49"), "received", 3),
]


class FakeConnection:
    """Answers every query with the same rows, as a mock does: it can't tell a
    renamed column from the real one."""

    def __init__(self, rows=ROWS, error=None):
        self.rows, self.error, self.queries = rows, error, []

    def run(self, sql):
        self.queries.append(sql)
        if self.error:
            raise self.error
        return [list(row) for row in self.rows]


class DatabaseError(Exception):
    """pg8000's error for a failed statement: its first argument holds Postgres's fields."""


class InterfaceError(Exception):
    """pg8000's error for a dropped connection."""


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DEPLOYED_AT", "26 Oct 2026, 09:30 UTC")
    monkeypatch.setattr(app, "_deployed_at", None)


@pytest.fixture
def database(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setitem(app._database, "url", "postgresql://orders_app:secret@db.example/orders")
    monkeypatch.setitem(app._database, "connection", connection)
    return connection


def request(path, method="GET"):
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "path": path}}}
    return app.handler(event, None)
