import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
SCRIPTS = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(SCRIPTS))

import run_state  # noqa: E402
# The shared host first, and this folder's reset: rehearse.py puts the Kubernetes demo's folder ahead of scripts/.
import host  # noqa: E402,F401
import reset  # noqa: E402,F401


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH
