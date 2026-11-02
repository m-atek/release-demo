"""The demo's k3s cluster on the tools host. Runs there as root.

    k3s.py up                 configure, start, set up the cluster, wait until it answers: "Ready"
    k3s.py down               stop k3s and every pod it runs, so it uses no memory
    k3s.py status             running or not, the releases' pods, the memory used
    k3s.py presenter CSR      sign the Mac's certificate request for the video's terminal (8 hours)
    k3s.py developer CSR      the same for Sam, video 26, in the group release-demo:developers and no other
    k3s.py audit --since UTC --namespace NAMESPACE --name NAME
                              the audit log's matching events as JSON lines: time, code, verb, resource,
                              subresource, name, namespace and user, and nothing else

scripts/demos/kubernetes/host.py runs it through SSM Run Command. The tools
host installs k3s at its first boot, but neither enables nor starts it: the
cluster runs only while demo 8 needs it.

up writes k3s's own configuration and nothing else on the host:

- /etc/rancher/k3s/config.yaml: one node; Traefik, ServiceLB, metrics-server
  and the local-path provisioner off; data on the data volume; pods held to
  PODS_MIB by the kubelet (system-reserved is the rest of the host's memory);
  the API server's audit log on, with audit-policy.yaml, at most 250 MB;
- /etc/rancher/k3s/registries.yaml: k3s pulls artifactory-demo.es.nl images
  from JCR on the host's local network, with a read-only token that expires
  after 12 hours, made afresh at each up;
- a drop-in that holds k3s itself (server, containerd) to K3S_MIB.
"""

import argparse
import base64
import datetime
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
AUDIT_POLICY = Path(DATA_DIR) / "server" / "audit.yaml"
AUDIT_LOG = Path(DATA_DIR) / "server" / "logs" / "audit.log"
DEVELOPERS = "release-demo:developers"
SECRETS = Path("/run/release-demo/secrets")
JCR = "http://127.0.0.1:8082"
REGISTRY_NAME = "artifactory-demo.es.nl"
PODS_MIB = 1280  # every pod together, kubelet-enforced
K3S_MIB = 768  # the k3s server and containerd
PULL_TOKEN_SECONDS = 12 * 3600


def run(*command, check=True, stdin=None):
    result = subprocess.run(command, capture_output=True, text=True, input=stdin)
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
        "kube-apiserver-arg": [f"audit-policy-file={AUDIT_POLICY}", f"audit-log-path={AUDIT_LOG}",
                               "audit-log-format=json", "audit-log-maxsize=50", "audit-log-maxbackup=4"],
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
    changed |= write_private(AUDIT_POLICY, (HERE / "audit-policy.yaml").read_text())
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
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
    # It finds them by the data folder, so it must know ours.
    if Path("/usr/local/bin/k3s-killall.sh").exists():
        subprocess.run(["/usr/local/bin/k3s-killall.sh"], capture_output=True,
                       env={**os.environ, "K3S_DATA_DIR": DATA_DIR})
    # Without pstree on the host, it kills the shims but not the containers under them;
    # the kernel stops whatever is left in the pods' cgroup.
    for name in ("kubepods.slice", "kubepods"):
        kill = Path("/sys/fs/cgroup") / name / "cgroup.kill"
        if kill.exists():
            kill.write_text("1")
    time.sleep(1)
    left = subprocess.run(["pgrep", "-f", f"{DATA_DIR}/data/"], capture_output=True, text=True).stdout.split()
    pods = [line for name in ("kubepods.slice", "kubepods")
            for f in (Path("/sys/fs/cgroup") / name).rglob("cgroup.procs") for line in f.read_text().split()]
    if left or pods:
        raise SystemExit(f"k3s stopped, but {len(left) + len(pods)} of its processes still run")
    print("k3s stopped, and every pod with it.")


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
    sign(csr_base64, f"release-demo-presenter-{int(time.time())}")


def subject_fields(line):
    """[(type, value), ...] from openssl's one-line subject, -nameopt RFC2253: a comma or a
    plus ends a field unless a backslash escapes it, so one value can't pose as two."""
    fields, field, escaped = [], "", False
    for char in line.strip().removeprefix("subject=") + ",":
        if escaped:
            field, escaped = field + char, False
        elif char == "\\":
            field, escaped = field + char, True
        elif char in ",+":
            kind, _, value = field.partition("=")
            fields.append((kind.strip(), value))
            field = ""
        else:
            field += char
    return fields


def developer(csr_base64):
    """Sam's certificate, for video 26: only for a request whose one group is DEVELOPERS, and
    whose name isn't one the cluster keeps for itself. Otherwise as presenter."""
    subject = run("openssl", "req", "-noout", "-subject", "-nameopt", "RFC2253",
                  stdin=base64.b64decode(csr_base64).decode())
    fields = subject_fields(subject)
    groups = [value for kind, value in fields if kind == "O"]
    names = [value for kind, value in fields if kind == "CN"]
    if groups != [DEVELOPERS] or len(names) != 1 or not names[0] or names[0].startswith("system:"):
        raise SystemExit(f"Not a developer's request: {subject.strip()}")
    sign(csr_base64, f"release-demo-developer-{int(time.time())}")


def sign(csr_base64, name):
    """Sign a certificate request from the Mac with the cluster's client CA, for 8 hours.
    The private key never leaves the Mac. Prints the certificate and the CA, both public."""
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


def moment(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def audit_files(since):
    """The audit log and its rotated copies that can hold events since then, oldest first."""
    files = sorted(AUDIT_LOG.parent.glob("audit*.log"), key=lambda path: path.stat().st_mtime)
    return [path for path in files if path.stat().st_mtime >= since.timestamp()]


def audit_row(event):
    """Only these fields leave the host. Never a request's or a response's body. The one thing read
    from a body is a made pod's name, because the log holds that name nowhere else."""
    reference = event.get("objectRef") or {}
    made = (event.get("responseObject") or {}).get("metadata") or {}
    return {"time": event.get("requestReceivedTimestamp"), "code": (event.get("responseStatus") or {}).get("code"),
            "verb": event.get("verb"), "resource": reference.get("resource"),
            "subresource": reference.get("subresource"), "name": reference.get("name") or made.get("name"),
            "namespace": reference.get("namespace"), "user": (event.get("user") or {}).get("username")}


def audit_rows(lines, since, namespace, name):
    """The matching rows: an object whose name equals or starts with name, and for peek also
    the Secret it mounts, whose refused read and whose read by the node the video shows."""
    words = [name, "catalog-db"] if name == "peek" else [name]
    for line in lines:
        if not any(word in line for word in words):
            continue
        try:
            row = audit_row(json.loads(line))
        except ValueError:
            continue  # the log's last line, while it is being written
        if row["namespace"] != namespace or not row["time"] or moment(row["time"]) < since:
            continue
        mounted = name == "peek" and (row["resource"], row["name"]) == ("secrets", "catalog-db")
        if mounted or (row["name"] or "").startswith(name):
            yield row


def audit(arguments):
    parser = argparse.ArgumentParser(prog="k3s.py audit")
    parser.add_argument("--since", required=True, help="UTC, for example 2026-11-02T13:22:04Z")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--name", required=True)
    options = parser.parse_args(arguments)
    since = moment(options.since)
    for path in audit_files(since):
        with open(path, encoding="utf-8", errors="replace") as log:
            for row in audit_rows(log, since, options.namespace, options.name):
                print(json.dumps(row))


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
    elif command == "developer" and len(sys.argv) == 3:
        developer(sys.argv[2])
    elif command == "audit":
        audit(sys.argv[2:])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
