"""Run a command on the cluster host with this checkout's demo 19 files, through SSM Run Command.

    python scripts/demos/argocd/host.py python3 scripts/demos/argocd/host/cluster.py status
    python scripts/demos/argocd/host.py --timeout 900 python3 scripts/demos/argocd/host/cluster.py up

The cluster host accepts no inbound connections, as demo 8's tools host
doesn't: so status, break-glass signing and rehearsals send their files and
a command through SSM, as scripts/demos/kubernetes/host.py does for the
tools host. The files (a few tens of KB, packed into the command itself) are
unpacked into a temporary folder that goes when the command ends. It runs
as root, with the host's Python environment, and prints what it printed.
At boot the host runs the same files from its own clone of main.
"""

import argparse
import base64
import io
import shlex
import sys
import tarfile
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from common import ROOT, aws_session, fail  # noqa: E402

NAME = "release-demo-cluster-host"
FILES = ["services.yaml", "people.yaml", "scripts/common.py", "scripts/demos/argocd"]
SKIP = {"tests", "__pycache__", ".pytest_cache"}
LIMIT = 90_000  # SSM takes 97 KB of parameters per command
VENV = "/opt/release-demo-venv/bin"


def bundle():
    """The files as a gzipped tar, base64: the same files give the same text."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in FILES:
            path = ROOT / name
            for item in sorted([path, *path.rglob("*")] if path.is_dir() else [path]):
                if item.is_file() and not SKIP & set(item.relative_to(ROOT).parts) and item.suffix != ".pyc":
                    info = archive.gettarinfo(str(item), item.relative_to(ROOT).as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = 0
                    with open(item, "rb") as file:
                        archive.addfile(info, file)
    return base64.b64encode(buffer.getvalue()).decode()


def script(command, files):
    """SSM runs commands with sh: this hands bash a script file."""
    body = "\n".join([
        "set -euo pipefail",
        "work=$(mktemp -d /tmp/release-demo-argocd.XXXXXX)",
        "trap 'rm -rf \"$work\"' EXIT",
        f"echo {files} | base64 -d | tar xz -C \"$work\"",
        "cd \"$work\"",
        f"export PATH={VENV}:$PATH KUBECONFIG=/etc/rancher/k3s/k3s.yaml AWS_DEFAULT_REGION=eu-west-1 PYTHONUNBUFFERED=1",
        f"{command} 2>&1",
    ])
    return (f"script=$(mktemp)\ncat > \"$script\" <<'RELEASE_DEMO_SCRIPT'\n{body}\nRELEASE_DEMO_SCRIPT\n"
            "bash \"$script\"; code=$?; rm -f \"$script\"; exit $code")


def instance(session, states=("running",)):
    """The cluster host's instance id and state, or None when it doesn't exist (yet)."""
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [NAME]},
        {"Name": "instance-state-name", "Values": list(states)}])
    found = [(i["InstanceId"], i["State"]["Name"]) for r in reply["Reservations"] for i in r["Instances"]]
    return found[0] if found else None


def run(command, timeout=600, session=None):
    """Run command on the cluster host; returns (exit code, output)."""
    files = bundle()
    if len(files) > LIMIT:
        fail(f"The files for the host are {len(files)} bytes as base64, over SSM's limit.")
    session = session or aws_session()
    ssm = session.client("ssm")
    host = instance(session)
    if not host:
        fail("The cluster host isn't running. Start it first: scripts/demo.sh up argocd")
    sent = ssm.send_command(InstanceIds=[host[0]], DocumentName="AWS-RunShellScript", TimeoutSeconds=60,
                            Parameters={"commands": [script(command, files)], "executionTimeout": [str(timeout)]})
    command_id = sent["Command"]["CommandId"]
    while True:
        time.sleep(3)
        try:
            reply = ssm.get_command_invocation(CommandId=command_id, InstanceId=host[0])
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if reply["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
    output = reply["StandardOutputContent"]
    if len(output) >= 24000:
        output += "\n(SSM returns the first 24,000 characters only)\n"
    return reply["ResponseCode"], output + reply["StandardErrorContent"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=int, default=600, help="seconds the command may run")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("name a command")
    code, output = run(" ".join(shlex.quote(part) for part in args.command), args.timeout)
    print(output, end="" if output.endswith("\n") else "\n")
    sys.exit(code if code >= 0 else 1)


if __name__ == "__main__":
    main()
