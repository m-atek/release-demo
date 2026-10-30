"""tools-host/host_secrets.py reads the catalog's parameters by name, ten at a
time, and a name the tools host's role may not read fails its whole
GetParameters call: the host wouldn't boot. So every name in tools_host.tf's
NotForTheHost deny is one the host never asks for."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools-host"))

import host_secrets  # noqa: E402


def denied_to_the_host():
    text = (ROOT / "infra" / "tools_host.tf").read_text()
    names = re.search(r'sid\s*=\s*"NotForTheHost".*?resources\s*=\s*\[for name in \[(.*?)\]', text, re.S).group(1)
    return set(re.findall(r'"([^"]+)"', names))


def test_the_host_never_asks_for_a_parameter_its_role_may_not_read():
    denied = denied_to_the_host()
    assert "servicenow/admin-password" in denied
    assert sorted(denied - host_secrets.NOT_ON_HOST) == []
