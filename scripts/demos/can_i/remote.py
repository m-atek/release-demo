"""Commands on the tools host, through video 8's host.py, with this demo's manifests in what it sends.

host.py packs a fixed list of files into every command, FILES. This adds the few
this demo needs, for this demo's own commands only, so video 8's commands stay
as they were. The cluster and SSM answer only from there; nothing here holds a
key or a token.
"""

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
# scripts/ci_env.py: scripts/common.py imports it, and k3s.py and on_host.py import common.
FILES = ["scripts/ci_env.py", "scripts/demos/can_i/on_host.py", "scripts/demos/can_i/cluster",
         "scripts/demos/can_i/take/before.yaml"]
K3S = "python3 scripts/demos/kubernetes/host/k3s.py"
ON_HOST = "python3 scripts/demos/can_i/on_host.py"


def load_host():
    """Video 8's host.py under a name of its own: scripts/ has a host.py too."""
    spec = importlib.util.spec_from_file_location("kubernetes_host", HERE.parent / "kubernetes" / "host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load_host()


def run(command, timeout=600):
    """Run command on the tools host; returns (exit code, output)."""
    usual = host.FILES
    host.FILES = [*usual, *(name for name in FILES if name not in usual)]
    try:
        return host.run(command, timeout)
    finally:
        host.FILES = usual
