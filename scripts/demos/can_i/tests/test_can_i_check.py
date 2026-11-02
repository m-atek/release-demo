"""check.py against a small stand-in for the cluster: the requests it makes, and what it calls a failure."""

import json
import re

import pytest

import check
import who
from check import Reply

VALUES = {"production": "demo-value-not-a-real-password", "staging": "staging-only-demo-value"}
LIST_AFTER = """Resources                                       Non-Resource URLs   Resource Names   Verbs
selfsubjectreviews.authentication.k8s.io        []                  []               [create]
selfsubjectaccessreviews.authorization.k8s.io   []                  []               [create]
selfsubjectrulesreviews.authorization.k8s.io    []                  []               [create]
events                                          []                  []               [get list watch]
pods/log                                        []                  []               [get list watch]
pods                                            []                  []               [get list watch]
deployments.apps                                []                  []               [get list watch]
replicasets.apps                                []                  []               [get list watch]
                                                [/api/*]            []               [get]
                                                [/healthz]          []               [get]
"""
EVERY_BINDING = {
    "rolebindings": [{"roleRef": {"kind": "ClusterRole", "name": "release-pipeline"},
                      "subjects": [{"kind": "ServiceAccount", "name": "release-pipeline", "namespace": "jenkins"}]},
                     {"roleRef": {"kind": "Role", "name": "developer"},
                      "subjects": [{"kind": "Group", "name": "release-demo:developers"}]}],
    "clusterrolebindings": [{"roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
                             "subjects": [{"kind": "Group", "name": "system:masters"}]},
                            {"roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
                             "subjects": [{"kind": "Group", "name": "release-demo:presenters"}]},
                            {"roleRef": {"kind": "ClusterRole", "name": "system:controller:deployment-controller"},
                             "subjects": [{"kind": "ServiceAccount", "name": "deployment-controller",
                                           "namespace": "kube-system"}]},
                            {"roleRef": {"kind": "ClusterRole", "name": "view"},
                             "subjects": [{"kind": "Group", "name": "system:authenticated"}]}],
    "roles": [{"metadata": {"name": "developer"},
               "rules": [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch"]}]}],
    "clusterroles": [{"metadata": {"name": "cluster-admin"},
                      "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]},
                     {"metadata": {"name": "release-pipeline"},
                      "rules": [{"apiGroups": ["apps"], "resources": ["deployments"],
                                 "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]}]},
                     {"metadata": {"name": "system:controller:deployment-controller"},
                      "rules": [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["update", "get"]}]},
                     {"metadata": {"name": "view"},
                      "rules": [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get"]}]}]}


class FakeProcess:
    def __init__(self, code, out, err):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_a_refusal_keeps_stderrs_words_and_json_only_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: FakeProcess(1, "out\n", "Forbidden\n"))
    cluster = check.Cluster(tmp_path)
    assert cluster.sam("get", "secrets") == Reply(1, "out\nForbidden")
    assert cluster.sam("get", "secrets", stdout_only=True) == Reply(1, "out")


def test_a_command_that_hangs_isnt_a_refusal(monkeypatch, tmp_path):
    def hang(*args, **options):
        raise check.subprocess.TimeoutExpired("kubectl", 30)

    monkeypatch.setattr(check.subprocess, "run", hang)
    assert not check.refused(check.Cluster(tmp_path).sam("attach", "pod", timeout=30))


def test_each_identity_has_its_own_kubeconfig(monkeypatch, tmp_path):
    used = []
    monkeypatch.setattr(check.subprocess, "run",
                        lambda *a, env, **k: used.append(env["KUBECONFIG"]) or FakeProcess(0, "", ""))
    cluster = check.Cluster(tmp_path)
    cluster.sam("get", "pods")
    cluster.admin("get", "pods")
    assert used == [str(tmp_path / "sam"), str(tmp_path / "config")]


class FakeCluster:
    """The cluster as the spec expects it. Each keyword breaks one thing, so a test can see it caught."""

    def __init__(self, labels=True, enforces=True, patch_left=False, staging_read_only=False, wrong_value=False,
                 empty_log=False, extra_deployer=None, sam_reads_secrets=False):
        self.calls, self.role = [], "before"
        self.labels, self.enforces, self.patch_left = labels, enforces, patch_left
        self.staging_read_only, self.wrong_value, self.empty_log = staging_read_only, wrong_value, empty_log
        self.extra_deployer, self.sam_reads_secrets = extra_deployer, sam_reads_secrets

    def sam(self, *args, **options):
        self.calls.append(("sam", args))
        return self.as_sam(args)

    def admin(self, *args, **options):
        self.calls.append(("admin", args, options.get("stdin")))
        return self.as_admin(args)

    def namespace(self, args):
        return args[args.index("-n") + 1] if "-n" in args else "production"

    def may_deploy(self, namespace):
        if namespace == "staging":
            return not self.staging_read_only
        return self.role == "before"

    def as_sam(self, args):
        namespace = self.namespace(args)
        if args[:2] == ("auth", "whoami"):
            return Reply(0, json.dumps({"status": {"userInfo": {"username": "sam", "groups": [
                "release-demo:developers", "system:authenticated"]}}}))
        if args[:3] == ("auth", "can-i", "--list"):
            return Reply(0, LIST_AFTER if not self.patch_left else LIST_AFTER + "deployments.apps  []  []  [patch]\n")
        if args[:2] == ("auth", "can-i"):
            if args[3] == "secrets":
                return Reply(1, "no")
            return Reply(0, "yes") if self.may_deploy(namespace) else Reply(1, "no")
        if "get" in args and ("secret" in args or "secrets" in args):
            if self.sam_reads_secrets:
                return Reply(0, "catalog-db Opaque")
            return Reply(1, 'Error from server (Forbidden): secrets "catalog-db" is forbidden: User "sam" cannot get')
        if "apply" in args:
            if self.may_deploy(namespace):
                return Reply(0, "deployment.apps/peek created")
            return Reply(1, 'Error from server (Forbidden): User "sam" cannot create resource "deployments"')
        if "rollout" in args:
            return Reply(0, "successfully rolled out")
        if "logs" in args and "deploy/peek" in args:
            return Reply(0, VALUES["production"] if self.wrong_value else VALUES[namespace])
        if "logs" in args:
            return Reply(0, "" if self.empty_log else "catalog 1.8.2 in production on :8080, ready: ok")
        if args[-3:] == ("-l", "app.kubernetes.io/instance=catalog", "-o") or "jsonpath={.items[0].metadata.name}" in args:
            return Reply(0, "catalog-6c9f7b5d44-abcde")
        if args[2:3] in (("get",), ("describe",)):
            return Reply(0, "fine")
        if "delete" in args and "peek" in args:
            return Reply(0, "deleted")
        if self.patch_left and ("patch" in args or "set" in args):
            return Reply(0, "deployment.apps/catalog patched")
        return Reply(1, "Error from server (Forbidden): forbidden")

    def as_admin(self, args):
        namespace = self.namespace(args)
        if args[:2] == ("apply", "-f") and args[2].endswith("after.yaml"):
            self.role = "after"
            return Reply(0, "role.rbac.authorization.k8s.io/developer configured")
        if args[:2] == ("apply", "-f") and args[2].endswith("before.yaml"):
            self.role = "before"
            return Reply(0, "role.rbac.authorization.k8s.io/developer configured")
        if args[:2] == ("get", "namespace"):
            labels = check.ENFORCE if self.labels else {}
            return Reply(0, json.dumps({"metadata": {"labels": labels}}))
        if args[0] == "create":
            return Reply(1, 'pods "x" is forbidden: violates PodSecurity "baseline:v1.36": hostPath volumes') \
                if self.enforces else Reply(0, "pod/hostpath-check-dry-run created (server dry run)")
        if "apply" in args:
            return Reply(0, "deployment.apps/hostpath-check created")
        if "events" in args:
            return Reply(0, json.dumps({"items": [{"involvedObject": {"name": "hostpath-check-5d4"},
                                                   "message": 'forbidden: violates PodSecurity "baseline:v1.36"'}]}))
        if "pods" in args and "app=hostpath-check" in args:
            return Reply(0, "")
        if args[0] == "get":
            kind = args[1]
            found = list(EVERY_BINDING[kind])
            if self.extra_deployer and kind == "rolebindings":
                found.append({"roleRef": {"kind": "ClusterRole", "name": "release-pipeline"},
                              "subjects": [{"kind": "User", "name": self.extra_deployer}]})
            return Reply(0, json.dumps({"items": found}))
        return Reply(0, f"fine in {namespace}")

    def sam_commands(self):
        return [call[1] for call in self.calls if call[0] == "sam"]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(check.time, "sleep", lambda seconds: None)


def test_a_good_cluster_passes_every_check(capsys):
    assert check.run_checks(FakeCluster()) == [True] * 5
    out = capsys.readouterr().out
    for number in ("1", "5", "3", "4", "6"):
        assert re.search(rf"^ok   {number}\. ", out, re.M)
    assert "FAIL" not in out


def test_each_check_prints_one_line_and_no_value(capsys):
    check.run_checks(FakeCluster())
    out = capsys.readouterr().out
    assert VALUES["production"] not in out and VALUES["staging"] not in out


def test_the_order_is_the_role_as_the_take_starts_then_the_fix():
    cluster = FakeCluster()
    check.run_checks(cluster)
    commands = cluster.sam_commands()
    first_apply = commands.index(("-n", "production", "apply", "-f", str(check.PEEK)))
    deleted = commands.index(("-n", "production", "delete", "deploy", "peek", "--ignore-not-found"))
    refused_apply = len(commands) - 1 - commands[::-1].index(("-n", "production", "apply", "-f", str(check.PEEK)))
    assert first_apply < deleted < refused_apply


def test_every_route_in_check_3_is_a_real_request_not_can_i():
    cluster = FakeCluster()
    cluster.role = "after"
    assert check.check_fix(cluster) == []
    made = {" ".join(args) for args in cluster.sam_commands()}
    for route in ("apply -f", "set image deploy/catalog catalog=busybox:1.37", "patch deploy catalog",
                  "scale deploy/catalog --replicas=4", "delete deploy catalog", "run can-i-check",
                  "create job can-i-check", "exec catalog-6c9f7b5d44-abcde", "attach catalog-6c9f7b5d44-abcde",
                  "port-forward catalog-6c9f7b5d44-abcde", "debug catalog-6c9f7b5d44-abcde", "create token default",
                  "get secret catalog-db", "get secrets", "get secrets --watch-only"):
        assert any(route in command for command in made), route


def test_a_route_left_open_fails_check_3(capsys):
    cluster = FakeCluster(patch_left=True)
    cluster.role = "after"
    problems = check.check_fix(cluster)
    assert "set image wasn't refused" in problems and "patch the pod template wasn't refused" in problems
    assert any("can-i --list shows deployments.apps ['patch']" in problem for problem in problems)


def test_a_secret_sam_can_read_fails_checks_1_and_3():
    cluster = FakeCluster(sam_reads_secrets=True)
    assert any("get secret catalog-db" in problem for problem in check.check_route(cluster))
    cluster.role = "after"
    assert "get secret wasn't refused" in check.check_fix(cluster)


def test_the_route_must_print_the_value():
    assert check.check_route(FakeCluster()) == []
    assert check.prints_value(FakeCluster(), "staging", "something-else") == [
        "peek's log in staging doesn't hold staging's value"]


def test_staging_must_never_print_productions_value():
    cluster = FakeCluster(wrong_value=True)
    problems = check.check_staging_and_labels(cluster)
    assert "staging's peek printed production's value" in problems


def test_the_alternative_sam_read_only_in_staging_is_understood():
    cluster = FakeCluster(staging_read_only=True)
    assert check.check_staging_and_labels(cluster) == []


def hostpath_applied(cluster):
    return any(call[0] == "admin" and str(check.HOSTPATH) in call[1] for call in cluster.calls)


def test_the_hostpath_file_goes_in_only_after_pod_security_refused_its_dry_run():
    cluster = FakeCluster()
    assert check.check_hostpath(cluster) == []
    dry = next(i for i, call in enumerate(cluster.calls) if call[1][:2] == ("create", "--dry-run=server"))
    applied = next(i for i, call in enumerate(cluster.calls) if str(check.HOSTPATH) in call[1])
    assert dry < applied


def test_without_the_labels_the_hostpath_file_never_goes_in():
    cluster = FakeCluster(labels=False)
    problems = check.check_hostpath(cluster)
    assert not hostpath_applied(cluster)
    assert any("runs only with the labels on" in problem for problem in problems)


def test_if_pod_security_doesnt_enforce_the_hostpath_file_never_goes_in():
    cluster = FakeCluster(enforces=False)
    problems = check.check_hostpath(cluster)
    assert not hostpath_applied(cluster)
    assert any("didn't refuse its dry run" in problem for problem in problems)


def test_the_dry_run_is_a_bare_pod_that_creates_nothing():
    pod = json.loads(check.dry_run_pod())
    assert pod["kind"] == "Pod" and "hostPath" in pod["spec"]["volumes"][0]
    assert pod["spec"]["nodeSelector"] == {"release-demo/never-scheduled": "true"}


def test_an_empty_catalog_log_fails_check_4():
    assert check.check_keeps(FakeCluster(empty_log=True)) == ["logs deploy/catalog printed nothing"]


def test_a_stranger_who_may_deploy_fails_check_6(capsys):
    assert check.check_deployers(FakeCluster()) == []
    assert check.check_deployers(FakeCluster(extra_deployer="pieter")) == ["pieter may deploy"]


@pytest.mark.parametrize("subject, own", [("system:serviceaccount:kube-system:generic-garbage-collector", True),
                                          ("system:kube-controller-manager", True),
                                          ("system:serviceaccount:default:someone", False),
                                          ("system:authenticated", False), ("system:serviceaccounts", False),
                                          ("system:serviceaccounts:production", False),
                                          ("sam", False), ("release-demo:developers", False)])
def test_what_counts_as_kubernetes_own(subject, own):
    assert check.kubernetes_own(subject) is own


def test_deployers_reads_wildcards_and_only_deploy_rights():
    def role(name, resource, verb):
        return {"metadata": {"name": name}, "rules": [{"apiGroups": ["apps"], "resources": [resource], "verbs": [verb]}]}

    roles = [role("writes", "deployments", "patch"), role("reads", "deployments", "get"),
             role("scale", "deployments/scale", "patch")]
    clusters = [{"metadata": {"name": "all"}, "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]}]
    bindings = [{"roleRef": {"kind": "Role", "name": name}, "subjects": [{"kind": "User", "name": name}]}
                for name in ("writes", "reads", "scale", "not-there")]
    wide = [{"roleRef": {"kind": "ClusterRole", "name": "all"}, "subjects": [{"kind": "Group", "name": "everything"}]},
            {"roleRef": {"kind": "ClusterRole", "name": "all"}}]
    assert check.deployers(roles, clusters, bindings, wide) == ["everything", "writes"]


def test_clean_up_removes_what_it_made_and_puts_the_role_back():
    cluster = FakeCluster()
    cluster.role = "after"
    check.clean_up(cluster)
    assert cluster.role == "before"
    deleted = [call[1] for call in cluster.calls if "delete" in call[1]]
    assert {d[1] for d in deleted} == {"staging", "production"}
    assert all("peek" in d and "hostpath-check" in d for d in deleted)


AUDIT = [{"time": "1", "code": 403, "verb": "get", "resource": "secrets", "name": "catalog-db", "user": "sam"},
         {"time": "2", "code": 201, "verb": "create", "resource": "deployments", "name": "peek", "user": "sam"},
         {"time": "3", "code": 201, "verb": "create", "resource": "pods", "name": "peek-5b7c9d6f4-x2x9q",
          "user": "system:serviceaccount:kube-system:replicaset-controller"},
         {"time": "4", "code": 200, "verb": "watch", "resource": "secrets", "name": "catalog-db",
          "user": "system:node:tools-host"},
         {"time": "5", "code": 200, "verb": "get", "resource": "pods", "name": "peek-5b7c9d6f4-x2x9q",
          "subresource": "log", "user": "sam"}]
CLEAN = {"sam_read_a_secret": 0, "body_on_a_secret": 0, "lines_with_a_fake_value": 0}


def test_check_8_passes_on_the_five_events_and_clean_counts():
    assert check.check_audit(AUDIT, CLEAN) == []


@pytest.mark.parametrize("missing, word", [(0, "403"), (1, "deployments/peek"), (2, "controller"), (3, "tools-host"),
                                            (4, "log")])
def test_check_8_names_a_missing_row(missing, word):
    problems = check.check_audit([row for i, row in enumerate(AUDIT) if i != missing], CLEAN)
    assert len(problems) == 1 and word in problems[0]


def test_check_8_takes_the_nodes_verb_as_the_log_has_it():
    for verb in ("get", "list", "watch"):
        rows = [{**row, "verb": verb} if row["user"].startswith("system:node") else row for row in AUDIT]
        assert check.check_audit(rows, CLEAN) == []


def test_check_8_fails_on_any_count():
    assert check.check_audit(AUDIT, {**CLEAN, "sam_read_a_secret": 1}) == ["raw log: sam_read_a_secret is 1"]


def test_check_8_reads_the_rows_who_prints(monkeypatch):
    monkeypatch.setattr(who, "read_mark", lambda: "2026-11-02T13:21:50Z")
    monkeypatch.setattr(who, "events", lambda name, mark: [*AUDIT, AUDIT[0]])
    assert check.audit_rows() == AUDIT


def test_the_listing_reader_ignores_reads_and_the_review_apis():
    assert check.writes_in(LIST_AFTER) == []
    assert check.writes_in(LIST_AFTER + "secrets  []  []  [get]\nsomething.apps  []  []  [create delete]\n") == [
        "something.apps ['create', 'delete']"]


def test_the_values_come_from_the_one_file_that_holds_them():
    assert check.fake_values() == VALUES
    assert "demo-value" not in (check.HERE / "check.py").read_text()
