"""Video 25's database in the demo's cluster. Runs on the tools host as the cluster admin.

    postgres.py up        catalog-postgres in staging and production, then waits until each is ready: "Ready"
    postgres.py down      removes what up made, by its label, and any block-database left behind
    postgres.py status    each database's pod, READY and RESTARTS, its products, and whether the block is on

scripts/demos/database-blip/up.sh, down.sh and status.sh run it through
scripts/demos/kubernetes/host.py. The password is one random value per
namespace, made once by up and kept in the Secret catalog-postgres. It reaches
kubectl on stdin, and nothing prints it.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kubernetes" / "host"))
import k3s  # noqa: E402  (it puts scripts/ on the path, where common lives)
from common import service  # noqa: E402

SERVICE = "catalog"
NAME = "catalog-postgres"
POD = f"{NAME}-0"
LABEL = "release-demo/demo=database-blip"
LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/instance": NAME, "release-demo/demo": "database-blip"}
POLICY = "block-database"
SEED = HERE.parents[2] / "demo-states" / SERVICE / "1.10.0" / "database" / "seed.sql"


def namespaces():
    return [environment["namespace"] for environment in service(SERVICE)["environments"].values()]


def running():
    return subprocess.run(["systemctl", "is-active", "--quiet", "k3s"]).returncode == 0


def apply(document, namespace):
    k3s.kubectl("apply", "--namespace", namespace, "-f", "-", stdin=json.dumps(document))


def password_secret(namespace):
    """Made once. It is the database's password from that database's first start on."""
    if k3s.kubectl("get", "secret", NAME, "--namespace", namespace, "--ignore-not-found", "-o", "name"):
        return False
    value = base64.urlsafe_b64encode(os.urandom(24)).decode()
    # create, not apply: apply would keep the value in an annotation, which a describe of the Secret prints.
    k3s.kubectl("create", "--namespace", namespace, "-f", "-", stdin=json.dumps(
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": NAME, "labels": LABELS}, "stringData": {"password": value}}))
    return True


def seed_map(namespace):
    apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{NAME}-seed", "labels": LABELS},
           "data": {"seed.sql": SEED.read_text()}}, namespace)


def pod_status(namespace):
    """(ready, restarts) of the database's pod, or None while there is none."""
    reply = k3s.kubectl("get", "pod", POD, "--namespace", namespace, "--ignore-not-found", "-o", "json")
    if not reply:
        return None
    statuses = json.loads(reply)["status"].get("containerStatuses") or [{}]
    return bool(statuses[0].get("ready")), statuses[0].get("restartCount", 0)


def rolled_out(namespace):
    return lambda: subprocess.run(["kubectl", "rollout", "status", f"statefulset/{NAME}", "--namespace", namespace,
                                   "--timeout=5s"], capture_output=True,
                                  env={**os.environ, "KUBECONFIG": str(k3s.ADMIN)}).returncode == 0


def products(namespace):
    """How many products this database holds, asked inside its pod."""
    return k3s.kubectl("exec", POD, "--namespace", namespace, "--", "psql", "-U", "catalog", "-d", "catalog", "-Atc",
                       "SELECT count(*) FROM products", check=False).strip() or "?"


def up():
    if not running():
        raise SystemExit("k3s isn't running. Start the cluster first: scripts/demo.sh up kubernetes")
    for namespace in namespaces():
        made = password_secret(namespace)
        seed_map(namespace)
        k3s.kubectl("apply", "--namespace", namespace, "-f", str(HERE / f"{NAME}.yaml"))
        print(f"  {namespace}: {NAME} applied{', its password made' if made else ''}", flush=True)
    for namespace in namespaces():
        k3s.wait(f"{namespace}: {POD}", rolled_out(namespace), 240)
        status = pod_status(namespace)
        if status and status[1]:
            # A k3s restart counts as one, and the video's top window shows this row at 0.
            print(f"  {namespace}: {POD} shows {status[1]} restarts from a k3s restart: a new pod, seeded again")
            k3s.kubectl("delete", "pod", POD, "--namespace", namespace)
            k3s.wait(f"{namespace}: {POD}", rolled_out(namespace), 240)
        print(f"  {namespace}: {products(namespace)} products")
    print("Ready")


def catalog_version(namespace):
    return k3s.kubectl("get", "deployment", SERVICE, "--namespace", namespace, "--ignore-not-found", "-o",
                       "jsonpath={.metadata.labels.app\\.kubernetes\\.io/version}", check=False).strip()


def down():
    if not running():
        print("k3s isn't running: nothing to remove now. Start it to remove catalog-postgres.")
        return
    for namespace in namespaces():
        version = catalog_version(namespace)
        k3s.kubectl("delete", "networkpolicy", POLICY, "--namespace", namespace, "--ignore-not-found")
        out = k3s.kubectl("delete", "statefulset,service,configmap,secret", "--namespace", namespace,
                          "--selector", LABEL, "--ignore-not-found")
        print(f"  {namespace}: {len(out.splitlines())} objects removed")
        if version.startswith("1.10."):
            print(f"  {namespace}: catalog {version} needs this database: "
                  "scripts/demos/kubernetes/reset.py --no-stage puts it on 1.8.2")
    print("Removed. Others' objects stay: this went by the label alone.")


def status():
    if not running():
        print("k3s: stopped, and catalog-postgres with it")
        return
    for namespace in namespaces():
        found = pod_status(namespace)
        if found is None:
            print(f"  {namespace}: no {NAME}. scripts/demo.sh up database-blip makes it.")
            continue
        block = k3s.kubectl("get", "networkpolicy", POLICY, "--namespace", namespace, "--ignore-not-found", "-o", "name")
        print(f"  {namespace}: {POD} ready {str(found[0]).lower()}, restarts {found[1]}, {products(namespace)} products"
              f"{', BLOCKED by ' + POLICY if block else ''}")


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the tools host, through scripts/demos/kubernetes/host.py.")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("up", "down", "status"):
        raise SystemExit(__doc__)
    {"up": up, "down": down, "status": status}[command]()


if __name__ == "__main__":
    main()
