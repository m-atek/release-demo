"""Demo 5's script tests, with fakes for the clouds, JFrog and SSM. Run them on their own:

    .venv/bin/python -m pytest -q scripts/demos/clouds/tests

(their modules build, check and deploy share names with scripts/, so they
don't run in one session with scripts/tests).
"""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.append(str(HERE.parents[2]))

import run_state  # noqa: E402


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    for variable in ("CLOUDS", "JENKINS_URL", "BUILD_URL", "OIDC_AWS_TOKEN", "OIDC_AZURE_TOKEN", "OIDC_GCP_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
