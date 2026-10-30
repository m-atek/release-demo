import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.append(str(ROOT / "scripts"))  # after the standard library: scripts/secrets.py would hide its secrets

import run_state  # noqa: E402

STATES = ROOT / "demo-states" / "runbook-assistant"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("RELEASE_DEMO_CACHE", str(tmp_path / "cache"))
    return tmp_path / "cache"


class FakeDatabricks:
    """An index that answers with the rows the test gives it."""

    def __init__(self, rows, ready=True):
        self.rows, self.ready, self.queries = rows, ready, []

    def index(self, name):
        return {"name": name, "status": {"ready": self.ready, "index_url": "idx.example.invalid/x"}} if self.ready else None

    def query(self, name, body, index_url=None):
        self.queries.append(body)
        columns = ["chunk_id", "page_id", "title", "status", "text", "score"]
        return {"manifest": {"columns": [{"name": c} for c in columns]},
                "result": {"data_array": [[r.get(c) for c in columns] for r in self.rows]}}
