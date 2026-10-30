"""The cluster host's k3s and Argo CD. Runs there as root.

    cluster.py up                       configure k3s, start it, set up Argo CD, wait until it answers: "Ready"
    cluster.py status                   k3s, the application's sync and health, the pods, the memory used
    cluster.py break-glass CSR NAME     sign a break-glass certificate request from the Mac, for an hour

boot.sh runs up at every start of the host; scripts/demos/argocd/host.py runs
the others from the Mac through SSM Run Command. up writes k3s's own
configuration and nothing else on the host:

- /etc/rancher/k3s/config.yaml: one node; Traefik, ServiceLB, metrics-server
  and the local-path provisioner off; the API server's audit log on, with
  audit-policy.yaml, rotated at 50 MB;
- /etc/rancher/k3s/registries.yaml: k3s pulls artifactory-demo.es.nl images
  from JCR on the tools host's private address, port 8082, with the
  read-only token demo 3 made, as the inventory servers do.

Then Argo CD: its install manifest at the pinned release, checked by its
SHA-256, with this demo's settings (argocd/kustomization.yaml), the
break-glass role, the drift recorder's token, and once, the pipeline
account's API token, generated into SSM as argocd/pipeline-token.
"""

import base64
import functools
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path("/etc/rancher/k3s")
ADMIN = CONFIG_DIR / "k3s.yaml"
SERVER_DIR = Path("/var/lib/rancher/k3s/server")
AUDIT_POLICY = SERVER_DIR / "audit.yaml"
AUDIT_LOG = SERVER_DIR / "logs" / "audit.log"
STATE = Path("/var/lib/release-demo")
RECORDER_TOKEN = Path("/etc/release-demo/recorder-token")
REGISTRY_NAME = "artifactory-demo.es.nl"
ARGOCD_VERSION = "v3.5.5"
ARGOCD_MANIFEST = f"https://raw.githubusercontent.com/argoproj/argo-cd/{ARGOCD_VERSION}/manifests/install.yaml"
ARGOCD_SHA256 = "90dc9094bf4c5964afd9c0e35d735e1c2fcbbda4f8e36799c092d9864ed65f50"
ARGOCD_PORT = 30080  # the NodePort, on the node's own address: kube-proxy may not serve it on localhost
APPLICATION = "catalog-production"
PIPELINE_ACCOUNT = "release-pipeline"
BREAK_GLASS_GROUP = "release-demo:break-glass"
BREAK_GLASS_SECONDS = 3600
SSM_PREFIX = "/release-demo/"


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


def ssm():
    import boto3

    return boto3.client("ssm", region_name="eu-west-1")


def parameter(name):
    """A platform value through the host's role, or None while unset."""
    try:
        value = ssm().get_parameter(Name=SSM_PREFIX + name, WithDecryption=True)["Parameter"]["Value"]
    except Exception:
        return None
    return None if value == "unset" else value


@functools.lru_cache(maxsize=1)
def settings():
    found = parameter("cluster-host/settings")
    if not found:
        raise SystemExit("cluster-host/settings isn't in SSM: apply infra/demos/19-argocd-hand-edit first.")
    return json.loads(found)


def write_private(path, text, mode=0o600):
    """Write a root-only file. Returns True when its content changed."""
    changed = not path.exists() or path.read_text() != text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=mode)
    path.chmod(mode)
    path.write_text(text)
    return changed


# k3s

def k3s_config():
    return {
        "node-name": "cluster-host",
        "write-kubeconfig-mode": "0600",
        "disable": ["traefik", "servicelb", "metrics-server", "local-storage"],
        "kube-apiserver-arg": [
            f"audit-policy-file={AUDIT_POLICY}",
            f"audit-log-path={AUDIT_LOG}",
            "audit-log-format=json",
            "audit-log-maxsize=50",
            "audit-log-maxbackup=4",
        ],
        "kubelet-arg": ["max-pods=60"],
    }


def token_subject(token, default="inventory-reader"):
    """The JCR user a JFrog access token belongs to, from its JWT subject (.../users/<name>)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims["sub"].rsplit("/", 1)[-1] or default
    except (IndexError, KeyError, ValueError):
        return default


def registries(jcr, token):
    """k3s's mirror of artifactory-demo.es.nl: JCR on the tools host's private address."""
    endpoint = jcr.split("//", 1)[1]
    return {"mirrors": {REGISTRY_NAME: {"endpoint": [jcr]}},
            "configs": {endpoint: {"auth": {"username": token_subject(token), "password": token}}}}


def configure(config):
    """k3s's configuration. Returns True when k3s must (re)start to pick it up."""
    token = parameter("jcr/inventory-read-token")
    if not token:
        raise SystemExit("jcr/inventory-read-token isn't set: run scripts/demos/linux/jcr_token.py once (demo 3).")
    changed = write_private(CONFIG_DIR / "config.yaml", yaml.safe_dump(k3s_config(), sort_keys=False))
    changed |= write_private(CONFIG_DIR / "registries.yaml", yaml.safe_dump(registries(config["jcr"], token)))
    changed |= write_private(AUDIT_POLICY, (HERE / "audit-policy.yaml").read_text())
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    return changed


def wait(what, check, timeout):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if check():
            print(f"  {what:<38} ok ({time.monotonic() - started:.0f}s)", flush=True)
            return
        time.sleep(3)
    raise SystemExit(f"  {what}: no answer after {timeout}s")


def node_ready():
    reply = kubectl("get", "node", "cluster-host", "-o", "json", check=False)
    if not reply:
        return False
    return any(c["type"] == "Ready" and c["status"] == "True" for c in json.loads(reply)["status"].get("conditions", []))


def rolled_out(kind, name, namespace="argocd"):
    return lambda: subprocess.run(["kubectl", "rollout", "status", f"{kind}/{name}", "-n", namespace, "--timeout=5s"],
                                  capture_output=True, env={**os.environ, "KUBECONFIG": str(ADMIN)}).returncode == 0


# Argo CD

def manifest(cache=STATE / "argocd"):
    """Argo CD's install manifest at the pinned release, downloaded once and checked by its SHA-256."""
    path = cache / f"install-{ARGOCD_VERSION}.yaml"
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != ARGOCD_SHA256:
        with urllib.request.urlopen(ARGOCD_MANIFEST, timeout=120) as response:
            data = response.read()
        actual = hashlib.sha256(data).hexdigest()
        if actual != ARGOCD_SHA256:
            raise SystemExit(f"{ARGOCD_MANIFEST} has SHA-256 {actual}, expected {ARGOCD_SHA256}. Not applying it.")
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return path


def render(config, install, out):
    """The kustomization with install.yaml beside it and this host's values filled in."""
    for item in (HERE / "argocd").iterdir():
        text = item.read_text()
        text = text.replace("__REPOSITORY__", f"https://github.com/{config['repository']}.git")
        text = text.replace("__NODE_ADDRESS__", config["address"])
        (Path(out) / item.name).write_text(text)
    shutil.copyfile(install, Path(out) / "install.yaml")
    result = subprocess.run(["kubectl", "kustomize", str(out)], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"kubectl kustomize failed: {result.stderr.strip()}")
    return result.stdout


def recorder_token():
    """The drift recorder's token, made once: the notifications controller sends it, the recorder checks it."""
    if not RECORDER_TOKEN.exists():
        write_private(RECORDER_TOKEN, secrets.token_urlsafe(32) + "\n")
    return RECORDER_TOKEN.read_text().strip()


def notifications_secret(token):
    current = kubectl("get", "secret", "argocd-notifications-secret", "-n", "argocd", "-o", "json", check=False)
    data = json.loads(current).get("data", {}) if current else {}
    if base64.b64decode(data.get("drift-recorder-token", "")).decode() == token:
        return
    patch = {"stringData": {"drift-recorder-token": token}}
    kubectl("patch", "secret", "argocd-notifications-secret", "-n", "argocd", "--type", "merge", "-p", json.dumps(patch))


def api(method, path, token=None, body=None):
    request = urllib.request.Request(f"http://{settings()['address']}:{ARGOCD_PORT}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, {}


def set_admin(enabled):
    kubectl("patch", "configmap", "argocd-cm", "-n", "argocd", "--type", "merge",
            "-p", json.dumps({"data": {"admin.enabled": "true" if enabled else "false"}}))


def pipeline_token():
    """release-pipeline's API token in SSM, made once. A stored token that still
    reads the application is kept. The admin account is on only while the
    token is made, and its password never leaves the cluster."""
    stored = parameter("argocd/pipeline-token")
    if stored and api("GET", f"/api/v1/applications/{APPLICATION}", stored)[0] == 200:
        print("  release-pipeline's token in SSM: still works")
        return
    secret = kubectl("get", "secret", "argocd-initial-admin-secret", "-n", "argocd", "-o", "json", check=False)
    if not secret:
        raise SystemExit("No argocd-initial-admin-secret: the admin password is unknown, so no token can be made.")
    password = base64.b64decode(json.loads(secret)["data"]["password"]).decode()
    set_admin(True)
    try:
        for _ in range(20):  # the server picks up argocd-cm within seconds
            status, session = api("POST", "/api/v1/session", body={"username": "admin", "password": password})
            if status == 200:
                break
            time.sleep(3)
        else:
            raise SystemExit(f"Argo CD refused the admin sign-in ({status}).")
        status, made = api("POST", f"/api/v1/account/{PIPELINE_ACCOUNT}/token", session["token"],
                           {"name": PIPELINE_ACCOUNT, "id": f"jenkins-{int(time.time())}"})
        if status != 200 or not made.get("token"):
            raise SystemExit(f"Argo CD didn't make release-pipeline's token ({status}).")
        ssm().put_parameter(Name=SSM_PREFIX + "argocd/pipeline-token", Value=made["token"], Type="SecureString",
                            Overwrite=True)
        print("  release-pipeline's token: made and stored in SSM as argocd/pipeline-token")
    finally:
        set_admin(False)


def argocd(config):
    # The namespace production and its break-glass role first: the application syncs into it at once.
    kubectl("apply", "-f", str(HERE / "break-glass.yaml"))
    with tempfile.TemporaryDirectory() as out:
        rendered = render(config, manifest(), out)
    kubectl("apply", "--server-side", "--force-conflicts", "-f", "-", stdin=rendered)
    notifications_secret(recorder_token())
    for kind, name in (("deployment", "argocd-redis"), ("deployment", "argocd-repo-server"),
                       ("statefulset", "argocd-application-controller"), ("deployment", "argocd-server"),
                       ("deployment", "argocd-notifications-controller")):
        wait(f"Argo CD {name}", rolled_out(kind, name), 300)
    wait("Argo CD API", lambda: api("GET", "/api/version")[0] == 200, 120)
    pipeline_token()


# Status

def application():
    reply = kubectl("get", "application", APPLICATION, "-n", "argocd", "-o", "json", check=False)
    return json.loads(reply) if reply else None


def app_line(app):
    if not app:
        return "catalog-production: not there yet"
    status = app.get("status", {})
    sync = status.get("sync", {})
    health = status.get("health", {}).get("status", "Unknown")
    revision = (sync.get("revision") or "")[:7]
    conditions = [c.get("message", "") for c in status.get("conditions", [])]
    line = f"catalog-production: {sync.get('status', 'Unknown')} at {revision or '?'}, {health}"
    return line + (f" ({conditions[0][:160]})" if conditions else "")


def memory():
    """MiB in use by k3s and by its pods, from their cgroups (page cache included)."""
    def current(path):
        file = Path("/sys/fs/cgroup") / path / "memory.current"
        return int(file.read_text()) // (1 << 20) if file.exists() else 0
    return current("system.slice/k3s.service"), max(current("kubepods.slice"), current("kubepods"))


def pods(namespace):
    reply = json.loads(kubectl("get", "pods", "-n", namespace, "-o", "json", check=False) or '{"items": []}')
    for pod in sorted(reply["items"], key=lambda p: p["metadata"]["name"]):
        statuses = pod["status"].get("containerStatuses") or [{}]
        version = pod["metadata"].get("labels", {}).get("app.kubernetes.io/version", "")
        yield pod["metadata"]["name"], version, bool(statuses[0].get("ready"))


def up():
    if not Path("/usr/local/bin/k3s").exists():
        raise SystemExit("k3s isn't installed. boot.sh installs it at the host's first boot.")
    config = settings()
    restart = configure(config)
    active = subprocess.run(["systemctl", "is-active", "--quiet", "k3s"]).returncode == 0
    print(f"k3s {run('k3s', '--version').split()[2]}, Argo CD {ARGOCD_VERSION}")
    if active and restart:
        print("  restarting k3s for the new configuration")
        run("systemctl", "restart", "k3s")
    elif not active:
        run("systemctl", "enable", "--now", "k3s")
    wait("node cluster-host Ready", node_ready, 240)
    wait("CoreDNS", rolled_out("deployment", "coredns", "kube-system"), 240)
    argocd(config)
    print(f"  {app_line(application())}")
    k3s_mib, pods_mib = memory()
    print(f"  memory: k3s {k3s_mib} MiB, pods {pods_mib} MiB")
    print("Ready")


def status():
    active = subprocess.run(["systemctl", "is-active", "k3s"], capture_output=True, text=True).stdout.strip()
    print(f"k3s: {active}")
    if active != "active":
        return
    print(f"  {app_line(application())}")
    for name, version, ready in pods("production"):
        print(f"  production  {name:<36} {version:<8} {'ready' if ready else 'not ready'}")
    k3s_mib, pods_mib = memory()
    print(f"  memory: k3s {k3s_mib} MiB, pods {pods_mib} MiB")
    recorder = subprocess.run(["systemctl", "is-active", "release-demo-recorder"], capture_output=True, text=True)
    print(f"  drift recorder: {recorder.stdout.strip()}")


def break_glass(csr_base64, name):
    """Sign a certificate request from the Mac with the cluster's client CA, for
    an hour. Only the request's own name and group count: the role break-glass
    binds the group. The key never leaves the Mac; this prints only the public
    certificate and the CA."""
    request = base64.b64decode(csr_base64)
    subject = run("openssl", "req", "-noout", "-subject", "-nameopt", "RFC2253", stdin=request.decode())
    if f"O={BREAK_GLASS_GROUP}" not in subject or "CN=" not in subject:
        raise SystemExit(f"Not a break-glass request: {subject.strip()}")
    csr = {"apiVersion": "certificates.k8s.io/v1", "kind": "CertificateSigningRequest",
           "metadata": {"name": name, "labels": {"release-demo/break-glass": "true"}},
           "spec": {"request": csr_base64, "signerName": "kubernetes.io/kube-apiserver-client",
                    "expirationSeconds": BREAK_GLASS_SECONDS, "usages": ["client auth"]}}
    kubectl("apply", "-f", "-", stdin=json.dumps(csr))
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
        raise SystemExit("Run as root on the cluster host: boot.sh, or scripts/demos/argocd/host.py from the Mac.")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "up":
        up()
    elif command == "status":
        status()
    elif command == "break-glass" and len(sys.argv) == 4:
        break_glass(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
