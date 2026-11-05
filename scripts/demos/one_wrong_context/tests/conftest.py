"""Nothing in these tests can reach AWS, SSM or an address but this machine's."""

import os
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
SCRIPTS = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.append(str(SCRIPTS))  # after the standard library: scripts/ has a secrets.py
sys.path.append(str(SCRIPTS / "demos" / "kubernetes"))

for name, value in {"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                    "AWS_EC2_METADATA_DISABLED": "true"}.items():
    os.environ[name] = value
os.environ.pop("AWS_PROFILE", None)

# The shared host first, and this folder's reset, as the entry points load them.
import host  # noqa: E402,F401
import reset  # noqa: E402,F401
import check  # noqa: E402,F401
import calls  # noqa: E402,F401
import contexts  # noqa: E402,F401
import demo  # noqa: E402,F401
import on_host  # noqa: E402,F401
import remote  # noqa: E402,F401
import window  # noqa: E402,F401


@pytest.fixture(autouse=True)
def only_this_machine(monkeypatch):
    """A connection anywhere else fails the test: a fixture once read a real key from SSM."""
    real = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"a test tried to reach {address}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    def refuse(*_, **__):
        raise AssertionError("a test asked for an AWS session")

    import common

    monkeypatch.setattr(common, "aws_session", refuse)
    monkeypatch.setattr(host, "aws_session", refuse)
    monkeypatch.setattr(demo, "aws_session", refuse)
