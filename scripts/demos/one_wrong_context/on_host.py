"""Video 32's steps on the tools host, as the cluster admin. Runs there as root.

    on_host.py remove     the policy and its binding, by name, and catalog back on its pods where a take left it short
    on_host.py status     what this demo has on the cluster. Changes nothing

scripts/demo.sh down and status run it through video 8's host.py; reset.py imports
it on the host. It deletes two objects, both named below, and nothing else: no
namespace, no pod, no Deployment. It reads what else runs, to say why a take can't
start, and never touches it: not video 25's Postgres, not video 33's forecast.
Standard library only.
"""

import json
import os
import subprocess
import sys

ADMIN = "/etc/rancher/k3s/k3s.yaml"
POLICY = BINDING = "no-zero"
DEPLOYMENT = "catalog"
REPLICAS = {"staging": 3, "production": 3}  # services.yaml, catalog's environments. A test holds them
WORKLOADS = "deployments,statefulsets,daemonsets"
OTHER_TEAM = "forecast"  # video 33's namespace


def kubectl(*args, check=True):
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, env={**os.environ, "KUBECONFIG": ADMIN})
    if check and result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args[:3])} failed: {result.stderr.strip()} "
                         "Is the cluster up? scripts/demo.sh up kubernetes")
    return result.stdout


def active():
    return subprocess.run(["systemctl", "is-active", "--quiet", "k3s"]).returncode == 0


def policy_on():
    return bool(kubectl("get", "validatingadmissionpolicy", POLICY, "--ignore-not-found", "-o", "name").strip())


def binding_on():
    return bool(kubectl("get", "validatingadmissionpolicybinding", BINDING, "--ignore-not-found", "-o", "name").strip())


def remove_policy():
    """The binding first: without it the policy applies to nothing. The API server takes about a second to notice."""
    kubectl("delete", "validatingadmissionpolicybinding", BINDING, "--ignore-not-found")
    kubectl("delete", "validatingadmissionpolicy", POLICY, "--ignore-not-found")


def replicas(namespace):
    """(wanted, ready) of catalog's Deployment, or None if the namespace has none."""
    reply = kubectl("get", "deployment", DEPLOYMENT, "-n", namespace, "--ignore-not-found", "-o", "json")
    if not reply.strip():
        return None
    document = json.loads(reply)
    return document["spec"].get("replicas", 1), document.get("status", {}).get("readyReplicas", 0)


def short_of_pods():
    """{namespace: what it wants now} where a take left catalog under its count: a scale to zero that never came back."""
    found = {}
    for namespace, wanted in REPLICAS.items():
        now = replicas(namespace)
        if now is not None and now[0] < wanted:
            found[namespace] = now[0]
    return found


def restore(namespace):
    """Before any Helm deploy: Helm 4 applies server-side, and a replica count kubectl set to another value is a conflict."""
    kubectl("scale", "deployment", DEPLOYMENT, "-n", namespace, f"--replicas={REPLICAS[namespace]}")


def others_in_production():
    names = kubectl("get", WORKLOADS, "-n", "production", "-o", "name").split()
    return [name for name in names if name != f"deployment.apps/{DEPLOYMENT}"]


def other_team_pods():
    return kubectl("get", "pods", "-n", OTHER_TEAM, "-o", "name", "--ignore-not-found").split()


def refusal():
    """Why a take can't start from here, or None. Reads only: these aren't this demo's to touch."""
    others = others_in_production()
    if others:
        return (f"production also runs {', '.join(others)}, and the take says every deployment there goes to zero "
                "and shows no pods left. Bring it down with its own demo: scripts/demo.sh down database-blip, for video 25's")
    if other_team_pods():
        return (f"video 33's {OTHER_TEAM} is up and fills the node, so the take's release has no room: "
                "scripts/demo.sh down hotfix-room first")
    return None


def remove():
    if not active():
        print("k3s isn't running: nothing to remove now. The policy stays on its disk until this runs with k3s up.")
        return
    remove_policy()
    print(f"Removed the ValidatingAdmissionPolicyBinding {BINDING} and the ValidatingAdmissionPolicy {POLICY}, if there.")
    for namespace in short_of_pods():
        restore(namespace)
        print(f"catalog in {namespace} back on {REPLICAS[namespace]} pods.")
    print("Nothing else touched. k3s stays up: scripts/demo.sh down kubernetes stops it.")


def status():
    if not active():
        print("k3s: not running")
        return
    print(f"policy {POLICY}: {'on' if policy_on() else 'off'}, binding {BINDING}: {'on' if binding_on() else 'off'}")
    for namespace in REPLICAS:
        now = replicas(namespace)
        print(f"catalog in {namespace}: " + (f"{now[0]} wanted, {now[1]} ready" if now else "not installed"))
    print(f"Why a take can't start: {refusal() or 'nothing in the way'}")


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the tools host, through scripts/demos/kubernetes/host.py.")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("remove", "status"):
        raise SystemExit(__doc__)
    {"remove": remove, "status": status}[command]()


if __name__ == "__main__":
    main()
