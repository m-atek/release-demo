"""scripts/demo.sh up, down and status for video 26, on video 8's cluster.

    scripts/demo.sh up can-i       refuses while demo 19's cluster host runs, then scripts/demo.sh up kubernetes
    scripts/demo.sh down can-i     removes the objects with this demo's label and any peek, then down kubernetes
    scripts/demo.sh status can-i   the role's state, the fake Secrets by name, the audit log, the labels,
                                   Sam's certificate's expiry. Never a value

This demo has no machine of its own: only k3s on the tools host, at no cost
beyond the tools host. demo.sh up argocd refuses the other way round.
"""

import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import remote  # noqa: E402
from common import ROOT, aws_session, fail  # noqa: E402

CLUSTER_HOST = "release-demo-cluster-host"
TOOLS_HOST = "release-demo-tools-host"
ONE_PRODUCTION = "One catalog production at a time: scripts/demo.sh down argocd first."
KUBECONFIG = Path.home() / ".kube" / "release-demo" / "sam"


def running(session, name):
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [name]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]}])
    return any(r["Instances"] for r in reply["Reservations"])


def video8(action):
    """Video 8's script, as demo.sh would run it."""
    return subprocess.run([str(ROOT / "scripts" / "demo.sh"), action, "kubernetes"]).returncode


def up():
    if running(aws_session(), CLUSTER_HOST):
        fail(ONE_PRODUCTION)
    return video8("up")


def down():
    if running(aws_session(), TOOLS_HOST):
        code, output = remote.run(f"{remote.ON_HOST} remove", timeout=120)
        print(output, end="" if output.endswith("\n") else "\n")
        if code != 0:
            fail("Couldn't remove this demo's objects. k3s is still running.")
    return video8("down")


def certificate_line(path=None):
    path = path or KUBECONFIG
    if not path.exists():
        return "Sam's certificate: none on this Mac (connect.py makes one)"
    config = json.loads(path.read_text())
    pem = base64.b64decode(config["users"][0]["user"]["client-certificate-data"]).decode()
    end = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem, capture_output=True,
                         text=True).stdout.strip().removeprefix("notAfter=")
    expires = datetime.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
    word = "expires" if expires > datetime.datetime.now(datetime.timezone.utc) else "expired"
    return f"Sam's certificate: {word} at {expires.astimezone():%H:%M:%S}"


def status():
    if running(aws_session(), TOOLS_HOST):
        code, output = remote.run(f"{remote.ON_HOST} status", timeout=60)
        print(output, end="" if output.endswith("\n") else "\n")
    else:
        code = 0
        print("k3s: stopped (the tools host is stopped)")
    print(certificate_line())
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    args = parser.parse_args()
    os.environ.setdefault("AWS_PROFILE", "release-demo")
    sys.exit({"up": up, "down": down, "status": status}[args.action]())


if __name__ == "__main__":
    main()
