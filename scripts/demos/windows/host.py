"""Run a small Python program on the tools host through SSM Run Command.

JCR answers only on the tools host (and, on 8082, to the store fleet), so the
steps that talk to it from the Mac run there: jcr_token.py and seed.py. The
program travels in the command as base64 and runs with the host's own venv
and role. It must not print secrets: its output shows in the command history.
"""

import base64
import sys
import time
from pathlib import Path

# The shared scripts, after anything already on the path: a demo module of the same name wins.
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parents[2]))
from common import aws_session, fail  # noqa: E402

VENV_PYTHON = "/opt/release-demo-venv/bin/python"


def tools_host(session):
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": ["release-demo-tools-host"]},
        {"Name": "instance-state-name", "Values": ["running"]}])
    hosts = [i["InstanceId"] for r in reply["Reservations"] for i in r["Instances"]]
    if not hosts:
        fail("The tools host isn't running. Start it: scripts/tools.sh up")
    return hosts[0]


def run_python(program, comment, args=(), files=None, timeout=300):
    """Runs program (Python source) on the tools host with args; files
    {name: bytes} land next to it. Returns (exit code, stdout, stderr)."""
    session = aws_session()
    ssm = session.client("ssm")
    host = tools_host(session)
    lines = ["set -e", "work=$(mktemp -d)", 'trap \'rm -rf "$work"\' EXIT']
    for name, content in {"program.py": program.encode(), **(files or {})}.items():
        lines.append(f"echo {base64.b64encode(content).decode()} | base64 -d > \"$work/{name}\"")
    lines.append(f'cd "$work" && {VENV_PYTHON} program.py {" ".join(args)}')
    command = ssm.send_command(InstanceIds=[host], DocumentName="AWS-RunShellScript", Comment=comment[:100],
                               Parameters={"commands": ["\n".join(lines)], "executionTimeout": [str(timeout)]}
                               )["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 60
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            reply = ssm.get_command_invocation(CommandId=command, InstanceId=host)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if reply["Status"] not in ("Pending", "InProgress", "Delayed"):
            return reply["ResponseCode"], reply["StandardOutputContent"], reply["StandardErrorContent"]
    fail(f"The tools host didn't finish {comment} in time (command {command}).")
