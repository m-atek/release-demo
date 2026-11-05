"""Commands on the tools host, through video 8's host.py, with this demo's few files in what it sends.

Works from any checkout, so the first-day check can run from a worktree before a merge.
Nothing here holds a key or a token.
"""

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["scripts/demos/one_wrong_context/on_host.py", "scripts/demos/one_wrong_context/calls.py"]
ON_HOST = "python3 scripts/demos/one_wrong_context/on_host.py"
CALLS = "python3 scripts/demos/one_wrong_context/calls.py"
K3S = "python3 scripts/demos/kubernetes/host/k3s.py"


def load_host():
    """Video 8's host.py under a name of its own: scripts/ has a host.py too."""
    spec = importlib.util.spec_from_file_location("kubernetes_host", HERE.parent / "kubernetes" / "host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load_host()


def run(command, timeout=600):
    """Run command on the tools host; returns (exit code, output)."""
    return host.run(command, timeout, folders=FILES)
