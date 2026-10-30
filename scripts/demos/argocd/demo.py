"""scripts/demo.sh up, down and status for demo 19: the cluster host.

    scripts/demo.sh up argocd       apply infra/demos/19-argocd-hand-edit on first use (plan shown first),
                                    start the host, wait for k3s and Argo CD: "Ready"
    scripts/demo.sh down argocd     stop the host (stop, not destroy: the cluster and Argo CD keep their state)
    scripts/demo.sh status argocd   the host, the application's sync and health, open drift incidents,
                                    and the break-glass certificate's expiry

up refuses while demo 8's k3s runs on the tools host: both are catalog's
production, and a take shows one. The host stops itself after 30 idle
minutes; its disk costs about USD 1.76 a month while it exists.
"""

import argparse
import base64
import calendar
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import host  # noqa: E402
from common import ROOT, aws_session, fail  # noqa: E402

TF = HERE / "tf.sh"
ONE_PRODUCTION = "One catalog production at a time: scripts/demo.sh down kubernetes first."
BOOT_TIMEOUT = 20 * 60
KUBECONFIG = Path.home() / ".kube" / "release-demo" / "pieter"


def tools_host(session):
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": ["release-demo-tools-host"]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    found = [(i["InstanceId"], i["State"]["Name"]) for r in reply["Reservations"] for i in r["Instances"]]
    return found[0] if found else None


def demo8_running(session):
    """Is k3s active on the tools host? Asked through SSM, only while the host runs."""
    found = tools_host(session)
    if not found or found[1] != "running":
        return False
    import importlib.util

    spec = importlib.util.spec_from_file_location("platform_host", ROOT / "scripts" / "host.py")
    platform = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(platform)
    code, out, _ = platform.Host(session).shell("systemctl is-active k3s || true", timeout=30)
    return out.strip() == "active"


def apply_root():
    """Terraform for the cluster host, the plan shown first and applied only after a yes."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="cluster-host-plan-") as folder:
        plan = Path(folder) / "cluster-host.tfplan"
        subprocess.run([str(TF), "plan", "-input=false", f"-out={plan}"], check=True)
        if input("Apply this plan? Type yes: ").strip() != "yes":
            fail("Not applied.")
        subprocess.run([str(TF), "apply", "-input=false", str(plan)], check=True)


def boot_section(lines, started):
    """This start's lines of the boot log: its newest boot, from the "== boot <UTC time>"
    line on, when that boot began after started (less a minute of clock skew). The log
    keeps every earlier boot, whose old errors would read as this one's; until this
    boot writes its first line, the newest boot in it is an older one, so nothing."""
    if not lines or not lines[0].startswith("== boot "):
        return []
    try:
        at = calendar.timegm(time.strptime(lines[0].split()[2], "%Y-%m-%dT%H:%M:%SZ"))
    except (IndexError, ValueError):
        return []
    return lines if at >= started - 60 else []


def wait_ready(session, started):
    """Until the host's boot writes cluster-ready, newer than this start. Prints this boot's progress."""
    instance = host.instance(session)
    ssm = session.client("ssm")
    deadline, shown = time.monotonic() + BOOT_TIMEOUT, 0
    while time.monotonic() < deadline:
        time.sleep(10)
        try:
            sent = ssm.send_command(InstanceIds=[instance[0]], DocumentName="AWS-RunShellScript", TimeoutSeconds=30,
                                    Parameters={"commands": [
                                        "cat /var/lib/release-demo/cluster-ready 2>/dev/null || echo 0; "
                                        "systemctl is-active release-demo-cluster-boot || true; "
                                        # the newest boot's lines, back to its "== boot <time>" line
                                        "tac /var/log/release-demo-cluster-boot.log 2>/dev/null | "
                                        "sed '/^== boot [0-9]/q' | tac"]})
        except ssm.exceptions.InvalidInstanceId:
            continue  # the agent isn't online yet
        time.sleep(3)
        try:
            reply = ssm.get_command_invocation(CommandId=sent["Command"]["CommandId"], InstanceId=instance[0])
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        lines = reply["StandardOutputContent"].splitlines()
        if len(lines) < 2:
            continue
        ready, boot = int(lines[0] or 0), lines[1]
        section = boot_section(lines[2:], started)
        for line in section[shown:]:
            print(f"  {line}")
        shown = max(shown, len(section))
        if ready >= started - 60 and boot == "active":
            return
        if boot == "failed":
            fail("The cluster host's boot failed: see /var/log/release-demo-cluster-boot.log there.")
    fail(f"The cluster host isn't ready after {BOOT_TIMEOUT // 60} minutes.")


def up(args):
    session = aws_session()
    if demo8_running(session):
        fail(ONE_PRODUCTION)
    found = host.instance(session, ("pending", "running", "stopping", "stopped"))
    if not found:
        print("No cluster host yet: Terraform makes it (infra/demos/19-argocd-hand-edit).")
        apply_root()
        found = host.instance(session, ("pending", "running"))
    # A host that runs already counts its own boot's marker; a started one, only a newer one.
    started = 0 if found[1] in ("pending", "running") else time.time()
    if found[1] in ("stopping", "stopped"):
        ec2 = session.client("ec2")
        if found[1] == "stopping":
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[found[0]])
        ec2.start_instances(InstanceIds=[found[0]])
        print(f"Starting the cluster host ({found[0]})")
        ec2.get_waiter("instance_running").wait(InstanceIds=[found[0]])
    tools = tools_host(session)
    if not tools or tools[1] != "running":
        print("The tools host is stopped: JCR there serves the catalog's image and cloudflared the UI. "
              "Start it too: scripts/tools.sh up")
    print("Waiting for k3s and Argo CD (about 3 minutes from stopped, 10 the first time)")
    wait_ready(session, started)
    code, output = host.run("python3 scripts/demos/argocd/host/cluster.py status", timeout=120, session=session)
    print(output.rstrip())
    print("Ready" if code == 0 else "Started, but the status above has a problem.")


def down(args):
    session = aws_session()
    found = host.instance(session, ("pending", "running", "stopping", "stopped"))
    if not found or found[1] in ("stopping", "stopped"):
        print("The cluster host is stopped." if found else "There is no cluster host.")
        return
    session.client("ec2").stop_instances(InstanceIds=[found[0]])
    print(f"Stopping the cluster host ({found[0]}). Its cluster and Argo CD keep their state for the next up.")


def certificate_line():
    if not KUBECONFIG.exists():
        return "break-glass: no certificate on this Mac (break_glass.py pieter makes one)"
    config = json.loads(KUBECONFIG.read_text())
    pem = base64.b64decode(config["users"][0]["user"]["client-certificate-data"]).decode()
    end = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem, capture_output=True,
                         text=True).stdout.strip().removeprefix("notAfter=")
    expires = datetime.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
    left = expires - datetime.datetime.now(datetime.timezone.utc)
    when = expires.astimezone().strftime("%H:%M:%S")
    return (f"break-glass: {config['users'][0]['name']}'s certificate valid until {when} "
            f"({int(left.total_seconds() // 60)} minutes left)" if left.total_seconds() > 0
            else f"break-glass: {config['users'][0]['name']}'s certificate expired at {when}")


def status(args):
    session = aws_session()
    found = host.instance(session, ("pending", "running", "stopping", "stopped"))
    print(f"cluster host: {found[1] if found else 'not created'}")
    if found and found[1] == "running":
        code, output = host.run("python3 scripts/demos/argocd/host/cluster.py status", timeout=120, session=session)
        print(output.rstrip())
    try:
        import change
        import gitops

        sn, _ = change.connect()
        incidents = gitops.open_drift_incidents(sn)
        print(f"open drift incidents: {', '.join(i['number'] for i in incidents) if incidents else 'none'}")
    except SystemExit:
        print("open drift incidents: ServiceNow didn't answer (asleep?)")
    print(certificate_line())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    args = parser.parse_args()
    os.environ.setdefault("AWS_PROFILE", "release-demo")
    {"up": up, "down": down, "status": status}[args.action](args)


if __name__ == "__main__":
    main()
