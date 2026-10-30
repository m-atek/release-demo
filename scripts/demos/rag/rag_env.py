"""The evaluation's own Python environment: MLflow 3 and its Databricks
client, pinned in scripts/demos/rag/requirements.txt.

evaluate.py calls ensure() first. (Not named venv: that is Python's own module.) Where those packages are missing, as in
the tools host's shared environment, it makes a virtual environment once,
named after the requirements' SHA-256, in the agent's cache on the data
volume, and runs itself again with it. Every other service's toolchain and
the host's own environment stay as they are.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = HERE / "requirements.txt"
MARKER = "RELEASE_DEMO_RAG_VENV"


def present():
    try:
        import anthropic  # noqa: F401
        import mlflow.genai  # noqa: F401
    except ImportError:
        return False
    return True


def folder():
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()[:12]
    base = Path(os.environ.get("RELEASE_DEMO_CACHE") or (HERE.parents[2] / ".local" / "cache"))
    if Path("/data/jenkins-agent/cache").is_dir():
        base = Path("/data/jenkins-agent/cache")
    return base / f"rag-venv-{digest}"


def ensure():
    """Run this process in the evaluation's environment, making it the first time."""
    if present():
        return
    if os.environ.get(MARKER):
        sys.exit("The evaluation's environment is missing MLflow or anthropic: delete it to make it again: "
                 f"{folder()}")
    target = folder()
    python = target / "bin" / "python"
    if not python.exists():
        print(f"Making the evaluation's environment once: {target} (a few minutes)", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                        "-r", str(REQUIREMENTS)], check=True)
    os.environ[MARKER] = "1"
    os.execv(str(python), [str(python), *sys.argv])
