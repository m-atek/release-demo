"""scripts/demo.sh down and status for video 32, on video 8's cluster.

    scripts/demo.sh down one-wrong-context [--yes]    deletes what's listed in WHAT, after your yes
    scripts/demo.sh status one-wrong-context          whether the policy is on, catalog's pods, what's in the way

There's no `up`: the demo has nothing of its own to start, and the video applies
the policy on camera. scripts/demo.sh up kubernetes brings the cluster.
"""

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import contexts  # noqa: E402
import remote  # noqa: E402
from common import aws_session, fail  # noqa: E402

TOOLS_HOST = "release-demo-tools-host"
WHAT = """This deletes, and only this:
  on the cluster: the ValidatingAdmissionPolicyBinding no-zero and the ValidatingAdmissionPolicy no-zero
  on this Mac: ~/.kube/release-demo/one-wrong-context
It also scales catalog back to 3 pods in any namespace a take left short. No namespace, pod or Deployment is deleted."""


def running(session=None):
    reply = (session or aws_session()).client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [TOOLS_HOST]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]}])
    return any(r["Instances"] for r in reply["Reservations"])


def down(yes=False, ask=input):
    print(WHAT, flush=True)
    if not yes and ask("Type yes: ").strip() != "yes":
        print("Nothing removed.")
        return 1
    if running():
        code, output = remote.run(f"{remote.ON_HOST} remove", timeout=120)
        print(output, end="" if output.endswith("\n") else "\n")
        if code != 0:
            fail("Couldn't remove this demo's objects. Nothing on this Mac was removed either.")
    else:
        print("The tools host is stopped: the policy stays on the cluster's disk until this runs with it up.")
    contexts.COPY.unlink(missing_ok=True)
    print(f"Removed {contexts.COPY}, if it was there.")
    return 0


def status():
    if not running():
        print("k3s: stopped (the tools host is stopped)")
        return 0
    code, output = remote.run(f"{remote.ON_HOST} status", timeout=60)
    print(output, end="" if output.endswith("\n") else "\n")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["down", "status"])
    parser.add_argument("--yes", action="store_true", help="down: don't ask")
    args = parser.parse_args()
    os.environ.setdefault("AWS_PROFILE", "release-demo")
    sys.exit(down(args.yes) if args.action == "down" else status())


if __name__ == "__main__":
    main()
