import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

import run_state  # noqa: E402
import stack  # noqa: E402


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    """Each test gets its own .release folder, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(stack, "WORK", tmp_path / ".release")
    monkeypatch.setattr(stack, "AWS_DIR", tmp_path / ".release" / "aws")
    return tmp_path / ".release"


def plan_with(*changes, drift=()):
    """A plan as terraform show -json writes it, with only what the scripts read."""
    return {
        "resource_changes": [{"address": a, "mode": "managed", "change": {"actions": actions}} for a, actions in changes],
        "resource_drift": list(drift),
    }
