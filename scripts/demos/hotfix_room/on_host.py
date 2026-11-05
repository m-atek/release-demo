"""Video 33's namespace in the demo's cluster. Runs on the tools host as the cluster admin.

    on_host.py up        the namespace forecast and the pipeline's rights in it: "Ready"
    on_host.py down      removes that namespace, and everything in it
    on_host.py status    the namespace, its pods, and the node's reading

scripts/demos/hotfix_room/up.sh, down.sh and status.sh run it through
scripts/demos/kubernetes/host.py. Others' namespaces aren't touched. While forecast's
2.4.0 runs the node has no room for a catalog rollout, so this comes up last and
goes down first: no other video's take runs beside it.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent / "kubernetes" / "host"))
import k3s  # noqa: E402  (it puts scripts/ on the path, where common lives)
import room  # noqa: E402

NAMESPACE = "forecast"


def running():
    return subprocess.run(["systemctl", "is-active", "--quiet", "k3s"]).returncode == 0


def present():
    return bool(k3s.kubectl("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name").strip())


def admin_json(*args):
    os.environ["KUBECONFIG"] = str(k3s.ADMIN)
    return room.kubectl_json(*args)


def up():
    if not running():
        raise SystemExit("k3s isn't running. Start the cluster first: scripts/demo.sh up kubernetes")
    print(k3s.kubectl("apply", "-f", str(HERE / "namespace.yaml")).strip())
    print("Ready")


def down():
    if not running():
        print("k3s isn't running: nothing to remove now. Start it to remove the namespace.")
        return
    if not present():
        print(f"No namespace {NAMESPACE}: nothing to remove.")
        return
    k3s.kubectl("delete", "namespace", NAMESPACE, "--wait=true", "--timeout=120s")
    print(f"Removed the namespace {NAMESPACE} and everything in it. Others' objects stay.")


def status():
    if not running():
        print("k3s: stopped, and forecast with it")
        return
    if not present():
        print(f"No namespace {NAMESPACE}. scripts/demo.sh up hotfix-room makes it.")
    else:
        print(k3s.kubectl("get", "pods", "-n", NAMESPACE, "-o",
                          "custom-columns=POD:.metadata.name,VERSION:.metadata.labels.app\\.kubernetes\\.io/version,"
                          "STATUS:.status.phase").strip())
    try:
        print("\n".join(room.frame(room.read(admin_json))))
    except (RuntimeError, KeyError, ValueError) as error:
        print(f"No reading: {error}")


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the tools host, through scripts/demos/kubernetes/host.py.")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("up", "down", "status"):
        raise SystemExit(__doc__)
    {"up": up, "down": down, "status": status}[command]()


if __name__ == "__main__":
    main()
