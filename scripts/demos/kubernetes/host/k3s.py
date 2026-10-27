"""The demo's k3s cluster on the tools host. Runs there as root.

    k3s.py up                 configure, start, set up the cluster, wait until it answers: "Ready"
    k3s.py down               stop k3s and every pod it runs, so it uses no memory
    k3s.py status             running or not, the releases' pods, the memory used
    k3s.py presenter CSR      sign the Mac's certificate request for the video's terminal (8 hours)

scripts/demos/kubernetes/host.py runs it through SSM Run Command. The tools
host installs k3s at its first boot, but neither enables nor starts it: the
cluster runs only while demo 8 needs it.

up writes k3s's own configuration and nothing else on the host:

- /etc/rancher/k3s/config.yaml: one node; Traefik, ServiceLB, metrics-server
  and the local-path provisioner off; data on the data volume; pods held to
  PODS_MIB by the kubelet (system-reserved is the rest of the host's memory);
- /etc/rancher/k3s/registries.yaml: k3s pulls artifactory-demo.es.nl images
  from JCR on the host's local network, with a read-only token that expires
  after 12 hours, made afresh at each up;
- a drop-in that holds k3s itself (server, containerd) to K3S_MIB.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from common import aws_session  # noqa: E402

CONFIG_DIR = Path("/etc/rancher/k3s")
DROP_IN = Path("/etc/systemd/system/k3s.service.d/release-demo.conf")
DATA_DIR = "/data/k3s"
ADMIN = CONFIG_DIR / "k3s.yaml"
SECRETS = Path("/run/release-demo/secrets")
JCR = "http://127.0.0.1:8082"
REGISTRY_NAME = "artifactory-demo.es.nl"
PODS_MIB = 1280  # every pod together, kubelet-enforced
K3S_MIB = 768  # the k3s server and containerd
PULL_TOKEN_SECONDS = 12 * 3600


def run(*command, check=True):
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"{' '.join(command[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def kubectl(*args, check=True, stdin=None):
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, input=stdin,
                            env={**os.environ, "KUBECONFIG": str(ADMIN)})
    if check and result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout


def secret(name):
    """A platform secret: from the boot's tmpfs, else SSM with the instance role."""
    path = SECRETS / name.replace("/", "-")
    if path.exists():
        return path.read_text().strip()
    try:
        value = aws_session().client("ssm").get_parameter(Name=f"/release-demo/{name}", WithDecryption=True)
    except Exception:
        return None
    value = value["Parameter"]["Value"]
    return None if value == "unset" else value


def memory_total_mib():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    raise SystemExit("No MemTotal in /proc/meminfo")


def k3s_config():
    reserved = memory_total_mib() - PODS_MIB
    return {
        "node-name": "tools-host",
        "data-dir": DATA_DIR,
        "write-kubeconfig-mode": "0600",
        "disable": ["traefik", "servicelb", "metrics-server", "local-storage"],
        "kubelet-arg": [f"system-reserved=memory={reserved}Mi", "kube-reserved=memory=0Mi",
                        "enforce-node-allocatable=pods", "eviction-hard=memory.available<100Mi", "max-pods=40"],
    }


def pull_token():
    """A read-only JCR token for the cluster's pulls: the readers group's rights,
    for a user that exists only in the token, valid 12 hours."""
    password = secret("jcr/admin-password")
    if not password:
        raise SystemExit("jcr/admin-password isn't set: JCR isn't set up yet (scripts/jcr_setup.py)")
    body = urllib.parse.urlencode({"username": "k3s-pull", "scope": "applied-permissions/groups:readers",
                                   "expires_in": str(PULL_TOKEN_SECONDS)}).encode()
    request = urllib.request.Request(f"{JCR}/artifactory/api/security/token", data=body, method="POST", headers={
        "Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode(),
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["access_token"]


def write_private(path, text):
    """Write a root-only file. Returns True when its content changed."""
    changed = not path.exists() or path.read_text() != text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(text)
    return changed


def configure(active):
    """k3s's configuration. Returns True when k3s must (re)start to pick up a change.
    A running k3s keeps the pull token it started with."""
    changed = write_private(CONFIG_DIR / "config.yaml", yaml.safe_dump(k3s_config(), sort_keys=False))
    if changed or not active:
        registries = {"mirrors": {REGISTRY_NAME: {"endpoint": [JCR]}},
                      "configs": {JCR.split("//")[1]: {"auth": {"username": "k3s-pull", "password": pull_token()}}}}
        write_private(CONFIG_DIR / "registries.yaml", yaml.safe_dump(registries))
    drop_in = ("[Service]\n# Demo 8: k3s itself (server and containerd) stays under this; "
               f"the kubelet holds the pods to {PODS_MIB} MiB.\nMemoryHigh={K3S_MIB - 64}M\nMemoryMax={K3S_MIB}M\n")
    if write_private(DROP_IN, drop_in):
        DROP_IN.chmod(0o644)
        run("systemctl", "daemon-reload")
        changed = True
    return changed


def wait(what, check, timeout):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if check():
            print(f"  {what:<34} ok ({time.monotonic() - started:.0f}s)", flush=True)
            return
        time.sleep(3)
    raise SystemExit(f"  {what}: no answer after {timeout}s")


def node_ready():
    reply = kubectl("get", "node", "tools-host", "-o", "json", check=False)
    if not reply:
        return False
    conditions = json.loads(reply)["status"].get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def rolled_out(namespace, name):
    return lambda: subprocess.run(["kubectl", "rollout", "status", f"deployment/{name}", "-n", namespace,
                                   "--timeout=5s"], capture_output=True,
                                  env={**os.environ, "KUBECONFIG": str(ADMIN)}).returncode == 0


def grafana():
    """Alloy, once the Grafana Cloud metrics parameters are set."""
    values = {key: secret(f"grafana/metrics-{key}") for key in ("url", "user", "token")}
    if not all(values.values()):
        print("  Grafana metrics                    not set up: set grafana/metrics-url, -user and -token "
              "(scripts/secrets.py set grafana)")
        return False
    manifest = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": "grafana-cloud", "namespace": "monitoring"}, "stringData": values}
    kubectl("apply", "-f", "-", stdin=json.dumps(manifest))
    kubectl("apply", "-f", str(HERE / "alloy.yaml"))
    return True


def memory():
    """MiB in use by k3s and by its pods, from their cgroups (page cache included)."""
    def current(path):
        file = Path("/sys/fs/cgroup") / path / "memory.current"
        return int(file.read_text()) // (1 << 20) if file.exists() else 0
    pods = max(current("kubepods.slice"), current("kubepods"))
    return current("system.slice/k3s.service"), pods


def up():
    if not Path("/usr/local/bin/k3s").exists():
        raise SystemExit("k3s isn't installed. The tools host installs it at its first boot (tools-host/boot.sh).")
    active = subprocess.run(["systemctl", "is-active", "--quiet", "k3s"]).returncode == 0
    restart = configure(active)
    print(f"k3s {run('k3s', '--version').split()[2]}: pods up to {PODS_MIB} MiB, k3s up to {K3S_MIB} MiB")
    if active and restart:
        print("  restarting k3s for the new configuration")
        run("systemctl", "restart", "k3s")
    elif not active:
        run("systemctl", "start", "k3s")
    wait("node tools-host Ready", node_ready, 180)
    kubectl("apply", "-f", str(HERE / "cluster.yaml"))
    with_grafana = grafana()
    wait("CoreDNS", rolled_out("kube-system", "coredns"), 180)
    wait("kube-state-metrics", rolled_out("monitoring", "kube-state-metrics"), 180)
    if with_grafana:
        wait("Grafana Alloy", rolled_out("monitoring", "alloy"), 180)
    k3s_mib, pods_mib = memory()
    print(f"  memory: k3s {k3s_mib} MiB, pods {pods_mib} MiB")
    print("Ready")


def down():
    if subprocess.run(["systemctl", "is-active", "--quiet", "k3s"]).returncode == 0:
        run("systemctl", "stop", "k3s")
    # Stopping the service leaves the pods' containers running; this stops them too.
    if Path("/usr/local/bin/k3s-killall.sh").exists():
        run("/usr/local/bin/k3s-killall.sh", check=False)
    k3s_mib, pods_mib = memory()
    print(f"k3s stopped. Memory left: k3s {k3s_mib} MiB, pods {pods_mib} MiB")


def status():
    active = subprocess.run(["systemctl", "is-active", "k3s"], capture_output=True, text=True).stdout.strip()
    print(f"k3s: {active}")
    if active != "active":
        return
    reply = json.loads(kubectl("get", "pods", "-A", "-o", "json"))
    for pod in sorted(reply["items"], key=lambda p: (p["metadata"]["namespace"], p["metadata"]["name"])):
        if pod["metadata"]["namespace"] not in ("staging", "production", "monitoring"):
            continue
        statuses = pod["status"].get("containerStatuses") or [{}]
        version = pod["metadata"].get("labels", {}).get("app.kubernetes.io/version", "")
        print(f"  {pod['metadata']['namespace']:<11} {pod['metadata']['name']:<40} {version:<8} "
              f"{'ready' if statuses[0].get('ready') else 'not ready'}")
    k3s_mib, pods_mib = memory()
    print(f"  memory: k3s {k3s_mib} MiB, pods {pods_mib} MiB (of {PODS_MIB + K3S_MIB} MiB allowed)")


def presenter(csr_base64):
    """Sign a certificate request from the Mac with the cluster's client CA, for 8 hours.
    The private key never leaves the Mac. Prints the certificate and the CA, both public."""
    name = f"release-demo-presenter-{int(time.time())}"
    request = {"apiVersion": "certificates.k8s.io/v1", "kind": "CertificateSigningRequest",
               "metadata": {"name": name},
               "spec": {"request": csr_base64, "signerName": "kubernetes.io/kube-apiserver-client",
                        "expirationSeconds": 8 * 3600, "usages": ["client auth"]}}
    kubectl("apply", "-f", "-", stdin=json.dumps(request))
    kubectl("certificate", "approve", name)
    certificate = ""
    for _ in range(20):
        certificate = kubectl("get", "csr", name, "-o", "jsonpath={.status.certificate}")
        if certificate:
            break
        time.sleep(1)
    if not certificate:
        raise SystemExit("The cluster didn't sign the request.")
    ca = kubectl("config", "view", "--raw", "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}")
    print(json.dumps({"certificate": certificate, "ca": ca}))


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the tools host, through scripts/demos/kubernetes/host.py.")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "up":
        up()
    elif command == "down":
        down()
    elif command == "status":
        status()
    elif command == "presenter" and len(sys.argv) == 3:
        presenter(sys.argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
