import os
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
SCRIPTS = HERE.parents[1]
KUBERNETES = SCRIPTS / "demos" / "kubernetes"

# No way to a real account from any test here.
os.environ.update({"AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
                   "AWS_EC2_METADATA_DISABLED": "true"})
os.environ.pop("AWS_PROFILE", None)

sys.path.insert(0, str(HERE))
sys.path.insert(1, str(SCRIPTS))

import run_state  # noqa: E402
# The shared host first, and this folder's reset: the Kubernetes demo has a host.py and a reset.py of its own.
import host  # noqa: E402,F401
import reset  # noqa: E402,F401

# Then its build, deploy and check, ahead of the Lambda ones in scripts/.
sys.path.insert(1, str(KUBERNETES))


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


@pytest.fixture(autouse=True)
def only_this_machine(monkeypatch):
    """A test that reaches for a real service fails at once, with the address it wanted."""
    connect = socket.socket.connect

    def local_only(self, address):
        if self.family == socket.AF_INET and address[0] not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"This test tried to reach {address}")
        if self.family == socket.AF_INET6:
            raise AssertionError(f"This test tried to reach {address}")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (_ for _ in ()).throw(
        AssertionError(f"This test tried to look up {host}")) if host not in ("127.0.0.1", "localhost", None) else
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", args[0] if args else 0))])
