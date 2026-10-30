import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

import run_state  # noqa: E402


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


@pytest.fixture
def told(monkeypatch):
    """What a stage would have said in Slack."""
    import trap

    lines = []
    monkeypatch.setattr(trap, "tell", lambda text: lines.append(text))
    return lines
