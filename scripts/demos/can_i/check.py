"""Acceptance checks 1 and 3 to 6 of video 26, by real requests with Sam's certificate, and check 8.

    .venv/bin/python scripts/demos/can_i/check.py            about 5 minutes, then run the reset
    .venv/bin/python scripts/demos/can_i/check.py --audit    check 8, after a pass of take 1

Needs connect.py running in another terminal, for the tunnels and both
certificates. One ok or FAIL line per check; it exits 1 if any fails, and prints
no value other than what a line says. It changes the cluster: it applies peek
in both namespaces, then after.yaml, and restarts catalog in both. Whatever
happens it removes what it made and puts before.yaml back. The audit log keeps
its requests, so run the reset before a take: scripts/reset.py can-i.

The hostPath half of check 5 asks Pod Security to refuse checks/hostpath.yaml. It
runs only with the labels on and after a dry run has been refused, and that
file couldn't reach the node's disk anyway: see its header. The other half, a
pod that really mounts the node's disk, never runs on the tools host.
"""

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import remote  # noqa: E402
import who  # noqa: E402
from common import fail  # noqa: E402

FOLDER = Path.home() / ".kube" / "release-demo"
PEEK = HERE / "take" / "peek.yaml"
BEFORE = HERE / "take" / "before.yaml"
AFTER = HERE / "take" / "after.yaml"
HOSTPATH = HERE / "checks" / "hostpath.yaml"
SECRETS = HERE / "cluster" / "secrets.yaml"
NAMESPACES = ("staging", "production")
DEVELOPERS = "release-demo:developers"
ENFORCE = {"pod-security.kubernetes.io/enforce": "baseline", "pod-security.kubernetes.io/enforce-version": "v1.36"}
REVIEW_APIS = {"selfsubjectreviews.authentication.k8s.io", "selfsubjectaccessreviews.authorization.k8s.io",
               "selfsubjectrulesreviews.authorization.k8s.io"}
EXPECTED_DEPLOYERS = {"system:serviceaccount:jenkins:release-pipeline", "release-demo:presenters", "system:masters"}
EVERYONE = {"system:authenticated", "system:unauthenticated", "system:serviceaccounts"}
NODE = "system:node:tools-host"
EVENT_SECONDS = 60

Reply = collections.namedtuple("Reply", "code text")


class Cluster:
    """kubectl as Sam and as the admin, each with its own kubeconfig."""

    def __init__(self, folder=FOLDER):
        self.configs = {"sam": folder / "sam", "admin": folder / "config"}

    def kubectl(self, who_asks, *args, stdin=None, timeout=90, stdout_only=False):
        """stderr too, for a refusal's words, unless only stdout will do: JSON, or whether a log is empty."""
        env = {**os.environ, "KUBECONFIG": str(self.configs[who_asks])}
        try:
            result = subprocess.run(["kubectl", *args], env=env, input=stdin, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired:
            return Reply(124, f"no answer after {timeout}s")
        return Reply(result.returncode, (result.stdout if stdout_only else result.stdout + result.stderr).strip())

    def sam(self, *args, **options):
        return self.kubectl("sam", *args, **options)

    def admin(self, *args, **options):
        return self.kubectl("admin", *args, **options)


def fake_values():
    """{namespace: value}, from the one file that holds them."""
    return {doc["metadata"]["namespace"]: doc["stringData"]["password"] for doc in yaml.safe_load_all(SECRETS.read_text())}


def refused(reply):
    return reply.code != 0 and "forbidden" in reply.text.lower()


def answer(reply):
    """The first word kubectl auth can-i printed: yes or no."""
    words = reply.text.split()
    return words[0] if words else ""


def check_route(cluster):
    """Check 1: the role answers no for Secrets, yes for Deployments, and a Deployment prints the value."""
    problems = []
    whoami = cluster.sam("auth", "whoami", "-o", "json", stdout_only=True)
    info = json.loads(whoami.text).get("status", {}).get("userInfo", {}) if whoami.code == 0 else {}
    if info.get("username") != "sam" or DEVELOPERS not in info.get("groups", []):
        problems.append(f"auth whoami names {info.get('username') or 'nobody'} in {info.get('groups')}")
    for verb in ("get", "list", "watch"):
        if answer(cluster.sam("auth", "can-i", verb, "secrets", "-n", "production")) != "no":
            problems.append(f"can-i {verb} secrets isn't no")
    read = cluster.sam("-n", "production", "get", "secret", "catalog-db")
    if not refused(read) or 'User "sam"' not in read.text:
        problems.append('get secret catalog-db wasn\'t Forbidden with User "sam"')
    if answer(cluster.sam("auth", "can-i", "create", "deployments", "-n", "production")) != "yes":
        problems.append("can-i create deployments isn't yes")
    problems += prints_value(cluster, "production", fake_values()["production"])
    return problems


def prints_value(cluster, namespace, value):
    """The same file as the take: applied, rolled out, and its log holds the namespace's value."""
    problems = []
    if cluster.sam("-n", namespace, "apply", "-f", str(PEEK)).code != 0:
        return [f"apply peek.yaml in {namespace} failed"]
    if cluster.sam("-n", namespace, "rollout", "status", "deploy/peek", "--timeout=120s", timeout=130).code != 0:
        problems.append(f"peek didn't roll out in {namespace}")
    if value not in cluster.sam("-n", namespace, "logs", "deploy/peek").text:
        problems.append(f"peek's log in {namespace} doesn't hold {namespace}'s value")
    return problems


def dry_run_pod():
    """A bare pod from the hostPath file's template, for a server-side dry run: nothing is created."""
    template = yaml.safe_load(HOSTPATH.read_text())["spec"]["template"]
    return json.dumps({"apiVersion": "v1", "kind": "Pod",
                       "metadata": {"name": "hostpath-check-dry-run", "labels": template["metadata"]["labels"]},
                       "spec": template["spec"]})


def pod_security_refusal(cluster, namespace):
    """The ReplicaSet's event says violates PodSecurity, and no pod exists. Waits for the event."""
    deadline = time.monotonic() + EVENT_SECONDS
    while True:
        reply = cluster.admin("-n", namespace, "get", "events", "--field-selector", "involvedObject.kind=ReplicaSet",
                              "-o", "json", stdout_only=True)
        for item in json.loads(reply.text).get("items", []) if reply.code == 0 else []:
            if (item["involvedObject"]["name"].startswith("hostpath-check-")
                    and "violates PodSecurity" in item.get("message", "")):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(3)


def check_hostpath(cluster):
    """Check 5's hostPath half: accepted as a Deployment, its pods refused. Never without the labels."""
    problems = []
    for namespace in NAMESPACES:
        reply = cluster.admin("get", "namespace", namespace, "-o", "json", stdout_only=True)
        labels = json.loads(reply.text)["metadata"].get("labels", {}) if reply.code == 0 else {}
        if any(labels.get(key) != value for key, value in ENFORCE.items()):
            problems.append(f"{namespace} has no Pod Security labels")
    if problems:
        return problems + ["the hostPath check didn't run: it runs only with the labels on"]
    for namespace in NAMESPACES:
        dry = cluster.admin("create", "--dry-run=server", "-n", namespace, "-f", "-", stdin=dry_run_pod())
        if "violates PodSecurity" not in dry.text:
            problems.append(f"a dry run of the hostPath pod wasn't refused in {namespace}")
    if problems:
        return problems + ["the hostPath check didn't run: Pod Security didn't refuse its dry run"]
    for namespace in NAMESPACES:
        if cluster.admin("-n", namespace, "apply", "-f", str(HOSTPATH)).code != 0:
            problems.append(f"the hostPath Deployment wasn't accepted in {namespace}")
            continue
        if not pod_security_refusal(cluster, namespace):
            problems.append(f"no violates PodSecurity event on the ReplicaSet in {namespace}")
        if "pod/" in cluster.admin("-n", namespace, "get", "pods", "-l", "app=hostpath-check", "-o", "name").text:
            problems.append(f"a hostPath pod exists in {namespace}")
        cluster.admin("-n", namespace, "delete", "deployment", "hostpath-check", "--ignore-not-found")
    return problems


def check_staging_and_labels(cluster):
    """Check 5: the same file in staging prints staging's value, Pod Security refuses hostPath,
    and catalog still rolls out under it."""
    values = fake_values()
    if answer(cluster.sam("auth", "can-i", "create", "deployments", "-n", "staging")) == "yes":
        problems = prints_value(cluster, "staging", values["staging"])
        if values["production"] in cluster.sam("-n", "staging", "logs", "deploy/peek").text:
            problems.append("staging's peek printed production's value")
    else:  # the alternative in cluster/developer.yaml: Sam read-only in staging
        refusal = cluster.sam("-n", "staging", "apply", "-f", str(PEEK))
        problems = [] if refused(refusal) else ["staging's apply wasn't refused"]
    problems += check_hostpath(cluster)
    for namespace in NAMESPACES:
        cluster.admin("-n", namespace, "rollout", "restart", "deploy/catalog")
        rolled = cluster.admin("-n", namespace, "rollout", "status", "deploy/catalog", "--timeout=180s", timeout=190)
        if rolled.code != 0:
            problems.append(f"catalog didn't roll out in {namespace} under Pod Security")
    return problems


def attempts(pod):
    """Check 3's requests as Sam, each in production. A real request every one, not can-i."""
    return {"apply peek.yaml": ("apply", "-f", str(PEEK)),
            "set image": ("set", "image", "deploy/catalog", "catalog=busybox:1.37"),
            "patch the pod template": ("patch", "deploy", "catalog", "--type=merge", "-p",
                                       '{"spec":{"template":{"metadata":{"labels":{"can-i-check":"true"}}}}}'),
            "scale": ("scale", "deploy/catalog", "--replicas=4"),
            "delete": ("delete", "deploy", "catalog"),
            "run": ("run", "can-i-check", "--image=busybox:1.37"),
            "create job": ("create", "job", "can-i-check", "--image=busybox:1.37"),
            "exec": ("exec", pod, "--", "true"),
            "attach": ("attach", pod),
            "port-forward": ("port-forward", pod, "18099:8080"),
            "debug": ("debug", pod, "--image=busybox:1.37"),
            "create token": ("create", "token", "default"),
            "get secret": ("get", "secret", "catalog-db"),
            "list secrets": ("get", "secrets"),
            "watch secrets": ("get", "secrets", "--watch-only", "--request-timeout=5s")}


def writes_in(listing):
    """What kubectl auth can-i --list allows beyond reading, the review APIs every user has aside."""
    found = []
    for line in listing.splitlines()[1:]:
        columns = re.findall(r"\[[^\]]*\]", line)
        if not columns:
            continue
        verbs = set(columns[-1].strip("[]").split()) - {"get", "list", "watch"}
        resource = line.split()[0] if not line.startswith(" ") else columns[0]
        if verbs and resource not in REVIEW_APIS:
            found.append(f"{resource} {sorted(verbs)}")
    return found


def check_fix(cluster):
    """Check 3: every route refused by a real attempt, with after.yaml on."""
    pod = cluster.sam("-n", "production", "get", "pods", "-l", "app.kubernetes.io/instance=catalog", "-o",
                      "jsonpath={.items[0].metadata.name}", stdout_only=True).text
    if not pod:
        return ["no catalog pod to try exec, attach, port-forward and debug against"]
    problems = [f"{name} wasn't refused" for name, args in attempts(pod).items()
                if not refused(cluster.sam("-n", "production", *args, timeout=30))]
    listing = cluster.sam("auth", "can-i", "--list", "-n", "production")
    if listing.code != 0:
        problems.append("can-i --list failed")
    problems += [f"can-i --list shows {write}" for write in writes_in(listing.text)]
    return problems


def check_keeps(cluster):
    """Check 4: what Sam keeps, by real commands. The output stays here."""
    problems = []
    for name, args in {"get pods": ("get", "pods"), "get deploy": ("get", "deploy"),
                       "describe deploy catalog": ("describe", "deploy", "catalog")}.items():
        if cluster.sam("-n", "production", *args).code != 0:
            problems.append(f"{name} failed")
    # stdout alone: with three pods, kubectl says which it used on stderr, even if the log is empty.
    log = cluster.sam("-n", "production", "logs", "deploy/catalog", "--tail=1", stdout_only=True)
    if log.code != 0 or not log.text:
        problems.append("logs deploy/catalog printed nothing")
    return problems


def grants_deploy(rule):
    groups, resources, verbs = rule.get("apiGroups", []), rule.get("resources", []), rule.get("verbs", [])
    return (("apps" in groups or "*" in groups) and ("deployments" in resources or "*" in resources)
            and bool({"create", "update", "patch", "*"} & set(verbs)))


def subject_name(subject):
    if subject["kind"] == "ServiceAccount":
        return f"system:serviceaccount:{subject['namespace']}:{subject['name']}"
    return subject["name"]


def deployers(roles, cluster_roles, role_bindings, cluster_role_bindings):
    """Every subject holding create, update or patch on deployments in production."""
    rules = {("Role", r["metadata"]["name"]): r.get("rules") or [] for r in roles}
    rules.update({("ClusterRole", r["metadata"]["name"]): r.get("rules") or [] for r in cluster_roles})
    found = set()
    for binding in [*role_bindings, *cluster_role_bindings]:
        reference = binding["roleRef"]
        if any(grants_deploy(rule) for rule in rules.get((reference["kind"], reference["name"]), [])):
            found.update(subject_name(subject) for subject in binding.get("subjects") or [])
    return sorted(found)


def kubernetes_own(subject):
    """Kubernetes' and k3s's own: its controllers and its system: users. Not a group that holds everyone."""
    if subject in EVERYONE or subject.startswith("system:serviceaccounts:"):
        return False
    if subject.startswith("system:serviceaccount:"):
        return subject.startswith("system:serviceaccount:kube-system:")
    return subject.startswith("system:")


def check_deployers(cluster):
    """Check 6: who can deploy in production, read from every binding. Sam and his group: absent."""
    def items(*args):
        return json.loads(cluster.admin("get", *args, "-o", "json", stdout_only=True).text)["items"]

    found = deployers(items("roles", "-n", "production"), items("clusterroles"),
                      items("rolebindings", "-n", "production"), items("clusterrolebindings"))
    named = [s for s in found if s in EXPECTED_DEPLOYERS or not kubernetes_own(s)]
    print(f"     may deploy in production: {', '.join(named)}, and {len(found) - len(named)} of Kubernetes' own")
    problems = [f"{s} may deploy" for s in found if s not in EXPECTED_DEPLOYERS and not kubernetes_own(s)]
    return problems + [f"{s} is missing from who may deploy" for s in sorted(EXPECTED_DEPLOYERS - set(found))]


def clean_up(cluster):
    """What this made goes, whatever happened, and production's Role is back as the take starts."""
    for namespace in NAMESPACES:
        cluster.admin("-n", namespace, "delete", "deployment", "peek", "hostpath-check", "--ignore-not-found")
    cluster.admin("apply", "-f", str(BEFORE))


def report(number, what, problems):
    why = f": {'; '.join(problems)}" if problems else ""
    print(f"{'FAIL' if problems else 'ok  '} {number}. {what}{why}", flush=True)
    return not problems


def run_checks(cluster):
    """1 and 5 with the role as the take starts, then 3, 4 and 6 with the fix on."""
    if cluster.admin("apply", "-f", str(BEFORE)).code != 0:
        fail("The admin's kubeconfig didn't reach the cluster. Is connect.py running?")
    results = [report(1, "The route is real", check_route(cluster)),
               report(5, "Staging, and Pod Security", check_staging_and_labels(cluster))]
    cluster.sam("-n", "production", "delete", "deploy", "peek", "--ignore-not-found")
    fixed = cluster.admin("apply", "-f", str(AFTER))
    if fixed.code != 0:
        results.append(report(3, "The fix closes this route", ["after.yaml wasn't applied"]))
        return results
    results += [report(3, "The fix closes this route, by real attempts", check_fix(cluster)),
                report(4, "What Sam keeps", check_keeps(cluster)),
                report(6, "Who can deploy", check_deployers(cluster))]
    return results


def audit_rows():
    return who.keep(who.events("peek", who.read_mark()), "peek")


def check_audit(rows, counts):
    """Check 8: the rows the script's 26.9 and 26.10 need, and the raw log's three counts."""
    wanted = [(lambda user, code, verb, target: user == "sam" and code == 403 and verb == "get"
               and target == "secrets/catalog-db", "no 403 for sam on secrets/catalog-db"),
              (lambda user, code, verb, target: user == "sam" and code == 201 and verb == "create"
               and target == "deployments/peek", "no 201 create by sam on deployments/peek"),
              (lambda user, code, verb, target: user != "sam" and code == 201 and verb == "create"
               and target.startswith("pods/peek-"), "no controller's 201 create on a peek pod"),
              (lambda user, code, verb, target: user == NODE and code == 200 and verb in ("get", "list", "watch")
               and target == "secrets/catalog-db",
               f"no 200 by {NODE} on secrets/catalog-db: 26.10 falls back, see the spec's check 8"),
              (lambda user, code, verb, target: user == "sam" and code == 200 and verb == "get"
               and target.startswith("pods/peek-") and target.endswith("/log"), "no 200 by sam on a peek pod's log")]
    problems = [text for test, text in wanted
                if not any(test(row.get("user"), row.get("code"), row.get("verb"), who.target(row)) for row in rows)]
    return problems + [f"raw log: {key} is {value}" for key, value in counts.items() if value]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", action="store_true", help="check 8, after a pass of take 1")
    args = parser.parse_args()
    if args.audit:
        code, output = remote.run(f"{remote.ON_HOST} log-check", timeout=120)
        if code != 0:
            fail(f"The raw audit log didn't answer: {output.strip()}")
        counts = json.loads(output.strip().splitlines()[-1])
        sys.exit(0 if report(8, "The audit log says what the script says", check_audit(audit_rows(), counts)) else 1)
    if not shutil.which("kubectl"):
        fail("No kubectl on this Mac.")
    cluster = Cluster()
    missing = [str(path) for path in cluster.configs.values() if not path.exists()]
    if missing:
        fail(f"Run connect.py first: no {', '.join(missing)}")
    try:
        results = run_checks(cluster)
    finally:
        clean_up(cluster)
    print("Before a take: .venv/bin/python scripts/reset.py can-i")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
