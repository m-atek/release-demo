"""Demo 3's own infrastructure, for scripts/demo.sh: up, down and status.

    scripts/demo.sh up linux       about 15 minutes the first time, 3 after
    scripts/demo.sh down linux     stops everything; nothing is deleted
    scripts/demo.sh status linux

up applies infra/demos/3-linux (the two cloud servers and what they use) and
starts them; makes or starts the three Multipass machines, inv-dc-01,
inv-dc-02 and dc-agent, and sets them up; connects dc-agent to Jenkins (out,
over WebSocket); runs the baseline on all four servers, with the drift on
inv-dc-01; puts the starting release, 2.7.3, on any server without one; and
checks that all four answer. Runs on the Mac: it needs Multipass, Docker and
your AWS sign-in. The cloud servers stop themselves 4 hours after boot.
"""

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import fleet  # noqa: E402
from common import ROOT, aws_session, fail, get_parameter, ssm_client  # noqa: E402

TERRAFORM = ROOT / "infra" / "demos" / "3-linux"
HERE = Path(__file__).resolve().parent
STARTING = "2.7.3"
BASELINE_OUT = ROOT / "dist" / "baseline"
JENKINS_PUBLIC = "https://jenkins-demo.es.nl"
VMS = {  # name: (cpus, memory, disk)
    "dc-agent": ("2", "2G", "10G"),
    "inv-dc-01": ("1", "1G", "6G"),
    "inv-dc-02": ("1", "1G", "6G"),
}
COST = "Two t3.small at USD 0.0228 an hour each, plus USD 0.005 an hour for each public address, while they run."


def step(text):
    print(f"\n== {text}", flush=True)


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


# Cloud


def terraform():
    bucket = get_parameter(ssm_client(), "terraform/state-bucket")
    run(["terraform", f"-chdir={TERRAFORM}", "init", "-input=false", f"-backend-config=bucket={bucket}"],
        stdout=subprocess.DEVNULL)
    plan = subprocess.run(["terraform", f"-chdir={TERRAFORM}", "plan", "-input=false", "-detailed-exitcode",
                           "-out=up.tfplan", "-no-color"], capture_output=True, text=True)
    if plan.returncode == 1:
        fail(plan.stderr or plan.stdout)
    if plan.returncode == 2:
        print("\n".join(line for line in plan.stdout.splitlines() if line.startswith(("  # ", "Plan:"))))
        run(["terraform", f"-chdir={TERRAFORM}", "apply", "-input=false", "up.tfplan"], stdout=subprocess.DEVNULL)
        print("Applied")
    else:
        print("No changes")
    (TERRAFORM / "up.tfplan").unlink(missing_ok=True)


def start_cloud():
    session = aws_session()
    ec2, ssm = session.client("ec2"), session.client("ssm")
    found = fleet.instances(session)
    ids = [found[s["name"]]["id"] for s in fleet.servers() if s["site"] == "cloud"]
    stopped = [i for i in ids if any(f["id"] == i and f["state"] in ("stopped", "stopping") for f in found.values())]
    if stopped:
        ec2.get_waiter("instance_stopped").wait(InstanceIds=stopped)
        ec2.start_instances(InstanceIds=stopped)
        print(f"Starting {', '.join(stopped)}")
    ec2.get_waiter("instance_running").wait(InstanceIds=ids)
    for _ in range(60):
        online = {i["InstanceId"] for i in ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": ids}])["InstanceInformationList"] if i["PingStatus"] == "Online"}
        if set(ids) <= online:
            print(f"Running and online in Systems Manager: {', '.join(ids)}")
            return
        time.sleep(5)
    fail("The cloud servers run but don't answer Systems Manager after 5 minutes")


# Data center


def vm_states():
    reply = subprocess.run(["multipass", "list", "--format", "json"], capture_output=True, text=True)
    if reply.returncode != 0:
        fail(f"Multipass doesn't answer: {reply.stderr.strip()}. Install it: brew install --cask multipass")
    return {vm["name"]: vm for vm in json.loads(reply.stdout)["list"]}


def start_vms():
    states = vm_states()
    for name, (cpus, memory, disk) in VMS.items():
        state = states.get(name, {}).get("state")
        if state is None:
            print(f"Making {name} (Ubuntu 24.04, {cpus} CPU, {memory} memory)")
            run(["multipass", "launch", "24.04", "--name", name, "--cpus", cpus, "--memory", memory, "--disk", disk,
                 "--timeout", "900"], stdout=subprocess.DEVNULL)
        elif state != "Running":
            print(f"Starting {name}")
            run(["multipass", "start", name], stdout=subprocess.DEVNULL)
    return {name: vm["ipv4"][0] for name, vm in vm_states().items() if name in VMS}


def exec_root(vm, script, stdin=b""):
    return subprocess.run(["multipass", "exec", vm, "--", "sudo", "bash", "-c", script],
                          input=stdin, capture_output=True)


def setup_agent():
    with tempfile.TemporaryDirectory() as temp:
        bundle = Path(temp) / "dc.tar"
        with tarfile.open(bundle, "w") as tar:
            for path in (HERE / "dc").iterdir():
                tar.add(path, arcname=path.name)
            tar.add(HERE / "ansible" / "requirements.txt", arcname="requirements.txt")
        reply = exec_root("dc-agent", "rm -rf /opt/dc-setup && mkdir -p /opt/dc-setup && tar -x -C /opt/dc-setup "
                                      "&& bash /opt/dc-setup/agent-setup.sh", bundle.read_bytes())
    output = reply.stdout.decode()
    if reply.returncode != 0:
        fail(f"dc-agent setup failed:\n{output}{reply.stderr.decode()}")
    for line in output.splitlines():
        if not line.startswith("PUBKEY "):
            print(f"  dc-agent: {line}")
    return next(line.removeprefix("PUBKEY ") for line in output.splitlines() if line.startswith("PUBKEY "))


def setup_servers(public_key, addresses):
    """The deploy user on each data-center server; then dc-agent knows them by name and host key."""
    known = []
    for server in fleet.servers():
        if server["site"] != "data-center":
            continue
        name = server["name"]
        reply = exec_root(name, (HERE / "dc" / "server-setup.sh").read_text().replace("key=$(cat)", f"key='{public_key}'"))
        if reply.returncode != 0:
            fail(f"{name} setup failed: {reply.stderr.decode()}")
        host_key = next(l.removeprefix("HOSTKEY ") for l in reply.stdout.decode().splitlines() if l.startswith("HOSTKEY "))
        known.append(f"{name},{addresses[name]} {host_key}")
        print(f"  {name}: {addresses[name]}, deploy user ready")
    hosts = "".join(f"{addresses[s['name']]} {s['name']}\n" for s in fleet.servers() if s["site"] == "data-center")
    script = ("sed -i '/# inventory demo$/d' /etc/hosts && "
              "while read -r ip name; do echo \"$ip $name # inventory demo\" >> /etc/hosts; done && "
              f"printf '%s\\n' {' '.join(repr(k) for k in known)} > {fleet.DC_HOME}/.ssh/known_hosts && "
              f"chown jenkins:jenkins {fleet.DC_HOME}/.ssh/known_hosts")
    reply = exec_root("dc-agent", script, hosts.encode())
    if reply.returncode != 0:
        fail(f"dc-agent hosts: {reply.stderr.decode()}")


def connect_agent():
    """dc-agent's arguments: Jenkins's URL, the node's secret and the Access
    service token, in a root-only file on dc-agent. Needs the dc-agent node in
    casc.yaml and its service token in Cloudflare; says what is missing if not."""
    ssm = ssm_client()
    values = {}
    for key in ("cloudflare/dc-agent-client-id", "cloudflare/dc-agent-client-secret"):
        try:
            values[key] = ssm.get_parameter(Name=f"/release-demo/{key}", WithDecryption=True)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            values[key] = "unset"
    if "unset" in values.values():
        print("  Not connected yet: dc-agent has no Cloudflare Access service token "
              "(cloudflare/dc-agent-client-id in SSM). See the README, demo 3.")
        return False
    headers = {"CF-Access-Client-Id": values["cloudflare/dc-agent-client-id"],
               "CF-Access-Client-Secret": values["cloudflare/dc-agent-client-secret"]}
    import base64

    admin = "Basic " + base64.b64encode(f"admin:{get_parameter(ssm, 'jenkins/admin-password')}".encode()).decode()
    request = urllib.request.Request(f"{JENKINS_PUBLIC}/computer/dc-agent/jenkins-agent.jnlp",
                                     headers={**headers, "Authorization": admin})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            jnlp = response.read().decode()
    except urllib.error.HTTPError as error:
        reason = "no dc-agent node in Jenkins yet (casc.yaml)" if error.code == 404 else f"Jenkins answered {error.code}"
        print(f"  Not connected yet: {reason}. See the README, demo 3.")
        return False
    except urllib.error.URLError as error:
        print(f"  Not connected yet: Jenkins doesn't answer ({error.reason}). Is the tools host up? scripts/tools.sh up")
        return False
    secret = re.search(r"\b[0-9a-f]{64}\b", jnlp)
    if not secret:
        fail("Jenkins gave no agent secret for dc-agent")
    args = ["-jar", f"{fleet.DC_HOME}/agent.jar", "-url", f"{JENKINS_PUBLIC}/", "-name", "dc-agent",
            "-secret", secret.group(0), "-webSocket",
            "-webSocketHeader", f"CF-Access-Client-Id={headers['CF-Access-Client-Id']}",
            "-webSocketHeader", f"CF-Access-Client-Secret={headers['CF-Access-Client-Secret']}",
            "-workDir", fleet.DC_HOME]
    reply = exec_root("dc-agent", "umask 077 && cat > /etc/jenkins-agent/agent.args.new && "
                                  "if cmp -s /etc/jenkins-agent/agent.args.new /etc/jenkins-agent/agent.args; "
                                  "then rm /etc/jenkins-agent/agent.args.new; else "
                                  "mv /etc/jenkins-agent/agent.args.new /etc/jenkins-agent/agent.args && "
                                  "systemctl restart jenkins-agent; fi && systemctl start jenkins-agent",
                      ("\n".join(args) + "\n").encode())
    if reply.returncode != 0:
        fail(f"dc-agent's agent service: {reply.stderr.decode()}")
    request = urllib.request.Request(f"{JENKINS_PUBLIC}/computer/dc-agent/api/json?tree=offline",
                                     headers={**headers, "Authorization": admin})
    for _ in range(30):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if not json.loads(response.read())["offline"]:
                    print("  dc-agent is connected to Jenkins, outbound over WebSocket")
                    return True
        except (urllib.error.URLError, ValueError):
            pass
        time.sleep(3)
    print("  dc-agent didn't connect within 90 seconds: multipass exec dc-agent -- journalctl -u jenkins-agent")
    return False


# Both


def dc_secrets():
    return {"INVENTORY_APP_SECRET": get_parameter(ssm_client(), "inventory/app-secret")}


def starting_package():
    """The starting release, built from demo-states in the Java toolchain once."""
    zip_path = BASELINE_OUT / "inventory" / f"inventory-{STARTING}.zip"
    info_path = BASELINE_OUT / "inventory" / "build-info.json"
    if not zip_path.exists() or not info_path.exists() or json.loads(info_path.read_text())["version"] != STARTING:
        print(f"Building inventory {STARTING} from demo-states in the Java toolchain")
        tag = "release-demo/java-tools:local"
        run(["docker", "build", "-q", "-t", tag, "-f", str(ROOT / "jenkins" / "toolchain" / "java.Dockerfile"),
             str(ROOT)], stdout=subprocess.DEVNULL)
        import os

        run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{ROOT}:/work", "-w", "/work",
             tag, "python", "scripts/demos/linux/build.py", "inventory", "--source",
             f"demo-states/inventory/{STARTING}", "--out", "dist/baseline"], stdout=subprocess.DEVNULL)
    info = json.loads(info_path.read_text())
    return {"source": "file", "file": str(zip_path), "sha256": info["sha256"]}


def baseline(drift=True):
    extra = {"java_drift_servers": [fleet.DRIFT_SERVER] if drift else [], "rollout_serial": 4}
    ok, _ = fleet.run("baseline.yml", extra, stop_on_failure=False, secrets=dc_secrets())
    if not ok:
        fail("The baseline failed on a server: see above")


def activate(version, package, names=None):
    extra = {"action": "activate", "version": version, "package": package, "rollout_serial": 4, "rollout_max_fail": 100}
    ok, results = fleet.run("release.yml", extra, names, stop_on_failure=False, secrets=dc_secrets())
    return ok, results


def without_release():
    """Servers with no current release, from a quick check."""
    from check import check

    return [name for name, ok, what in check(STARTING) if not ok and "no answer" in what or "answered 404" in what]


def up(args):
    step("Cloud servers: Terraform (infra/demos/3-linux)")
    terraform()
    start_cloud()
    step("Data center: Multipass machines")
    addresses = start_vms()
    print("  " + ", ".join(f"{name} {ip}" for name, ip in addresses.items()))
    public_key = setup_agent()
    setup_servers(public_key, addresses)
    step("dc-agent: connect out to Jenkins")
    connect_agent()
    step("Baseline on all four servers: Java 11 (Java 8 on inv-dc-01, the planted drift), Tomcat 9")
    baseline(drift=not args.no_drift)
    step(f"The starting release, {STARTING}, where a server has none")
    package = starting_package()
    ok, results = activate(STARTING, package, names=None if args.activate_all else without_release() or [])
    step("Every server's /health")
    status_lines()
    print(f"\nReady. {COST}")


def status_lines():
    from check import check

    rows = check(STARTING)
    for name, ok, what in rows:
        print(f"  {name:<14} {what}")


def down(args):
    session = aws_session()
    found = fleet.instances(session)
    running = [f["id"] for f in found.values() if f["state"] in ("pending", "running")]
    if running:
        session.client("ec2").stop_instances(InstanceIds=running)
        print(f"Stopping the cloud servers: {', '.join(running)}")
    else:
        print("The cloud servers are already stopped")
    states = vm_states()
    names = [name for name in VMS if states.get(name, {}).get("state") == "Running"]
    if names:
        run(["multipass", "stop", *names])
        print(f"Stopped {', '.join(names)}")
    print("Stopped. Nothing is deleted: scripts/demo.sh up linux starts it again.")


def status(args):
    session = aws_session()
    found = fleet.instances(session)
    for server in fleet.servers():
        if server["site"] == "cloud":
            state = found.get(server["name"], {}).get("state", "not made")
            print(f"  {server['name']:<14} EC2 {state}")
    states = vm_states() if subprocess.run(["which", "multipass"], capture_output=True).returncode == 0 else {}
    for name in VMS:
        print(f"  {name:<14} Multipass {states.get(name, {}).get('state', 'not made')}")
    if "Running" == states.get("dc-agent", {}).get("state"):
        agent = exec_root("dc-agent", "systemctl is-active jenkins-agent").stdout.decode().strip()
        print(f"  {'dc-agent':<14} agent service {agent}")
    everything = all(found.get(s["name"], {}).get("state") == "running" for s in fleet.servers() if s["site"] == "cloud") \
        and all(states.get(n, {}).get("state") == "Running" for n in VMS)
    if everything:
        print("Servers:")
        status_lines()
    if any(f["state"] == "running" for f in found.values()):
        print(COST)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    upper = sub.add_parser("up")
    upper.add_argument("--no-drift", action="store_true", help="Java 11 on inv-dc-01 too")
    upper.add_argument("--activate-all", action="store_true", help=f"put {STARTING} on every server")
    sub.add_parser("down")
    sub.add_parser("status")
    args = parser.parse_args()
    {"up": up, "down": down, "status": status}[args.command](args)


if __name__ == "__main__":
    main()
