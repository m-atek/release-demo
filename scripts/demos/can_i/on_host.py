"""This demo's steps on the tools host, as the cluster admin. Runs there as root, through remote.py.

    on_host.py prepare     for a take: no peek, cluster/ and take/before.yaml applied, the image on the node
    on_host.py remove      what this demo put on the cluster: the objects with its label, and any peek
    on_host.py status      the role's state, the fake Secrets by name, the audit log, the labels
    on_host.py log-check   acceptance check 8 in the raw audit log, as counts

Nothing here asks the cluster for a Secret's value or prints one: Secrets are
asked for by name only, and log-check prints only how many lines it found.
"""

import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kubernetes" / "host"))
import k3s  # noqa: E402

NAMESPACES = ("staging", "production")
LABEL = "release-demo/demo=can-i"
DEPLOYMENTS = ("peek", "hostpath-check")
PODS = "app in (peek,hostpath-check)"
IMAGE = "busybox:1.37"
# busybox's sh is PID 1 and doesn't stop on SIGTERM, so a pod takes its 30 seconds' grace to go.
GONE_SECONDS = 90
ENFORCE = "pod-security.kubernetes.io/enforce"
ENFORCE_VERSION = "pod-security.kubernetes.io/enforce-version"
# Decision 72's defaults, which a take stops without. Prep 26's table says what else changes if one goes.
NEEDS_AUDIT_LOG = True
NEEDS_POD_SECURITY = True


def labels(namespace):
    """(enforce level, its version) on the namespace, or None for either that's missing."""
    reply = k3s.kubectl("get", "namespace", namespace, "-o", "json", check=False)
    found = json.loads(reply)["metadata"].get("labels", {}) if reply else {}
    return found.get(ENFORCE), found.get(ENFORCE_VERSION)


def audit_on():
    """k3s has the flags and the policy. The API server took them at its last start: up restarts it."""
    config = k3s.CONFIG_DIR / "config.yaml"
    return (config.exists() and k3s.AUDIT_POLICY.exists()
            and f"audit-policy-file={k3s.AUDIT_POLICY}" in config.read_text())


def pods_left():
    return [line for namespace in NAMESPACES
            for line in k3s.kubectl("get", "pods", "-n", namespace, "-l", PODS, "-o", "name").split()]


def wait_gone():
    """A pod still going would add its delete events after the mark, and to the next take's audit log."""
    deadline = time.monotonic() + GONE_SECONDS
    while pods_left():
        if time.monotonic() >= deadline:
            raise SystemExit(f"Pods still there after {GONE_SECONDS}s: {', '.join(pods_left())}")
        time.sleep(2)


def mark():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def image_on_node():
    """A pull asks Docker Hub even for an image the node has; a retake shouldn't depend on it answering."""
    return subprocess.run(["k3s", "crictl", "inspecti", IMAGE], capture_output=True).returncode == 0


def prepare():
    if NEEDS_AUDIT_LOG and not audit_on():
        raise SystemExit("The audit log isn't on: scripts/demo.sh up can-i first.")
    for namespace in NAMESPACES:
        if NEEDS_POD_SECURITY and labels(namespace)[0] != "baseline":
            raise SystemExit(f"{namespace} has no Pod Security label: scripts/demo.sh up can-i first.")
    for namespace in NAMESPACES:
        k3s.kubectl("delete", "deployment", *DEPLOYMENTS, "-n", namespace, "--ignore-not-found")
    wait_gone()
    print(k3s.kubectl("apply", "-f", str(HERE / "cluster"), "-f", str(HERE / "take" / "before.yaml")).strip())
    # The file on screen carries no label; down finds the Role by this one. A later apply keeps it.
    k3s.kubectl("label", "role", "developer", "-n", "production", LABEL, "--overwrite")
    if not image_on_node():
        k3s.run("k3s", "crictl", "pull", IMAGE)
    print(f"{IMAGE} on the node")
    print(f"mark {mark()}")


def remove():
    if not active():
        print("k3s isn't running: nothing to remove.")
        return
    for namespace in NAMESPACES:
        k3s.kubectl("delete", "secret,role,rolebinding,deployment", "-n", namespace, "-l", LABEL, "--ignore-not-found")
        k3s.kubectl("delete", "deployment", "peek", "-n", namespace, "--ignore-not-found")
    print("Removed what this demo put on the cluster: the objects with its label, and any peek.")


def active():
    return subprocess.run(["systemctl", "is-active", "--quiet", "k3s"]).returncode == 0


def role_state(rules):
    """before: the three lines are there. after: no write at all. Anything else says so."""
    writes = {verb for rule in rules for verb in rule.get("verbs", []) if verb not in ("get", "list", "watch")}
    deploys = any(verb in ("create", "update", "patch", "delete") and "deployments" in rule.get("resources", [])
                  for rule in rules for verb in rule.get("verbs", []))
    if deploys and writes == {"create", "update", "patch", "delete"}:
        return "before (with deploy rights)"
    return "after (read only)" if not writes else f"other: writes {sorted(writes)}"


def status():
    if not active():
        print("k3s: not running")
        return
    reply = k3s.kubectl("get", "role", "developer", "-n", "production", "-o", "json", check=False)
    print(f"role developer in production: {role_state(json.loads(reply)['rules']) if reply else 'not there'}")
    # -o name: never the Secret itself.
    secrets = [namespace for namespace in NAMESPACES
               if k3s.kubectl("get", "secret", "catalog-db", "-n", namespace, "-o", "name", "--ignore-not-found").strip()]
    print(f"fake Secret catalog-db in: {', '.join(secrets) or 'neither namespace'}")
    size = sum(path.stat().st_size for path in k3s.AUDIT_LOG.parent.glob("audit*.log"))
    print(f"audit log: {'on' if audit_on() else 'off'}, {size / (1 << 20):.1f} MB of at most 250 MB")
    for namespace in NAMESPACES:
        level, version = labels(namespace)
        print(f"Pod Security in {namespace}: {level or 'no label'}{f' {version}' if version else ''}")


def log_counts(lines, values):
    """Check 8 in the raw log. All three must be 0."""
    counts = {"sam_read_a_secret": 0, "body_on_a_secret": 0, "lines_with_a_fake_value": 0}
    for line in lines:
        counts["lines_with_a_fake_value"] += any(value in line for value in values)
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (event.get("objectRef") or {}).get("resource") == "secrets":
            code = (event.get("responseStatus") or {}).get("code") or 0
            counts["sam_read_a_secret"] += (event.get("user") or {}).get("username") == "sam" and 200 <= code < 300
            counts["body_on_a_secret"] += "requestObject" in event or "responseObject" in event
    return counts


def log_check():
    values = [doc["stringData"]["password"] for doc in yaml.safe_load_all((HERE / "cluster" / "secrets.yaml").read_text())]
    totals = {}
    since = datetime.datetime.fromtimestamp(0, datetime.timezone.utc)
    for path in k3s.audit_files(since):
        with open(path, encoding="utf-8", errors="replace") as log:
            for key, count in log_counts(log, values).items():
                totals[key] = totals.get(key, 0) + count
    print(json.dumps(totals or log_counts([], values)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["prepare", "remove", "status", "log-check"])
    args = parser.parse_args()
    {"prepare": prepare, "remove": remove, "status": status, "log-check": log_check}[args.step]()


if __name__ == "__main__":
    main()
