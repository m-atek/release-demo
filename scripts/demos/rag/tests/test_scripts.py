"""Each script starts on its own, as Jenkins and the Mac run it: its imports resolve without the
tests' paths. --help stops before any call to AWS, Databricks or Claude."""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]


@pytest.mark.parametrize("script", ["answer.py", "build.py", "check.py", "deploy.py", "evaluate.py", "index.py",
                                    "search.py", "setup.py", "reset.py"])
def test_the_script_starts(script):
    result = subprocess.run([sys.executable, str(HERE / script), "--help"], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
