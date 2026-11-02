"""The files on screen and what goes on the cluster, read as the cluster and the video read them."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import on_host
from conftest import baseline_violations

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
TAKE = HERE / "take"
CLUSTER = HERE / "cluster"
COLUMNS = 56
LABEL = {"release-demo/demo": "can-i"}
THREE_LINES = ["  - apiGroups: [apps]", "    resources: [deployments]", "    verbs: [create, update, patch, delete]"]
READ_RULES = [{"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["get", "list", "watch"]},
              {"apiGroups": [""], "resources": ["pods", "pods/log", "events"], "verbs": ["get", "list", "watch"]}]
DEPLOY_RULE = {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["create", "update", "patch", "delete"]}
FORBIDDEN_RESOURCES = {"secrets", "pods/exec", "pods/attach", "pods/portforward", "pods/ephemeralcontainers",
                       "serviceaccounts/token", "serviceaccounts", "statefulsets", "daemonsets", "jobs", "cronjobs",
                       "pods/*", "*"}


def lines(name):
    return (TAKE / name).read_text().splitlines()


def documents(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text()) if d]


def granted(rules, *verbs):
    return {resource for rule in rules for resource in rule["resources"] if set(verbs) & set(rule["verbs"])}


def test_after_is_before_without_its_last_three_lines():
    before, after = lines("before.yaml"), lines("after.yaml")
    assert before[-3:] == THREE_LINES
    assert before[:-3] == after


def test_gits_diff_removes_those_three_lines_and_nothing_else(tmp_path):
    for name in ("before.yaml", "after.yaml"):
        shutil.copy(TAKE / name, tmp_path / name)
    # As the take runs it, in that folder: exit 1 only says they differ.
    result = subprocess.run(["git", "diff", "--no-index", "-U99", "before.yaml", "after.yaml"], cwd=tmp_path,
                            capture_output=True, text=True)
    body = result.stdout.splitlines()
    removed = [line[1:] for line in body if line.startswith("-") and not line.startswith("---")]
    added = [line for line in body if line.startswith("+") and not line.startswith("+++")]
    assert removed == THREE_LINES and added == []
    assert " metadata: { name: developer, namespace: production }" in body
    assert max(len(line) for line in body) <= COLUMNS


@pytest.mark.parametrize("name", ["before.yaml", "after.yaml", "peek.yaml"])
def test_the_files_on_screen_fit_the_terminal(name):
    assert max(len(line) for line in lines(name)) <= COLUMNS


def test_the_role_in_each_file():
    before, after = yaml.safe_load((TAKE / "before.yaml").read_text()), yaml.safe_load((TAKE / "after.yaml").read_text())
    assert before["rules"] == [*READ_RULES, DEPLOY_RULE] and after["rules"] == READ_RULES
    for role in (before, after):
        assert role["kind"] == "Role"
        assert role["metadata"] == {"name": "developer", "namespace": "production"}


@pytest.mark.parametrize("name", ["before.yaml", "after.yaml"])
def test_neither_file_grants_secrets_the_exec_family_or_another_workload(name):
    rules = yaml.safe_load((TAKE / name).read_text())["rules"]
    assert not {resource for rule in rules for resource in rule["resources"]} & FORBIDDEN_RESOURCES
    assert all("*" not in rule["verbs"] and "*" not in rule["apiGroups"] for rule in rules)
    # The only writes are the three lines', on deployments, never on a subresource or another kind.
    assert granted(rules, "create", "update", "patch", "delete") <= {"deployments"}


def test_sams_read_access_stays_inside_kubernetes_view_role():
    # view reads these and no Secret: the role's reads are a subset of it, by name.
    view = {"pods", "pods/log", "events", "deployments", "replicasets"}
    assert {resource for rule in READ_RULES for resource in rule["resources"]} == view


def test_peek_is_the_file_the_video_shows():
    assert len(lines("peek.yaml")) == 20
    deployment = yaml.safe_load((TAKE / "peek.yaml").read_text())
    assert deployment["kind"] == "Deployment" and deployment["metadata"] == {"name": "peek"}  # no namespace: -n says it
    spec = deployment["spec"]["template"]["spec"]
    assert spec["volumes"] == [{"name": "s", "secret": {"secretName": "catalog-db"}}]
    container = spec["containers"][0]
    assert container["args"] == ["cat /s/password; echo; sleep 3600"] and container["command"] == ["sh", "-c"]
    assert container["volumeMounts"] == [{"name": "s", "mountPath": "/s"}]
    assert container["image"] == on_host.IMAGE  # what the reset pulls, so a first pull never lands on the clock


def test_peek_passes_baseline():
    assert baseline_violations(yaml.safe_load((TAKE / "peek.yaml").read_text())["spec"]["template"]["spec"]) == []


def test_the_baseline_helper_really_refuses():
    spec = yaml.safe_load((HERE / "checks" / "hostpath.yaml").read_text())["spec"]["template"]["spec"]
    assert baseline_violations(spec) == ["hostPath volume host"]
    assert baseline_violations({"hostNetwork": True, "containers": [
        {"name": "c", "securityContext": {"privileged": True}, "ports": [{"hostPort": 80}]}]})


def versions():
    return sorted(path.name for path in (ROOT / "demo-states" / "catalog").iterdir() if (path / "chart").is_dir())


@pytest.mark.skipif(not shutil.which("helm"), reason="helm isn't installed")
@pytest.mark.parametrize("version", versions())
def test_catalogs_chart_passes_baseline(version):
    result = subprocess.run(["helm", "template", "catalog", str(ROOT / "demo-states" / "catalog" / version / "chart"),
                             "--namespace", "production", "--set", "image.digest=sha256:" + "ab" * 32],
                            capture_output=True, text=True, check=True)
    deployment = next(d for d in yaml.safe_load_all(result.stdout) if d and d["kind"] == "Deployment")
    assert baseline_violations(deployment["spec"]["template"]["spec"]) == []
    text = result.stdout
    assert "catalog-db" not in text  # catalog mounts neither Secret


def test_the_hostpath_file_couldnt_reach_the_disk():
    """The route it stands for must never run on the tools host, even with Pod Security off."""
    deployment = yaml.safe_load((HERE / "checks" / "hostpath.yaml").read_text())
    spec = deployment["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"release-demo/never-scheduled": "true"}  # no node has it, so it stays Pending
    assert spec["volumes"] == [{"name": "host", "hostPath": {"path": "/nonexistent/can-i-check", "type": "Directory"}}]
    assert spec["containers"][0]["command"] == ["true"] and "args" not in spec["containers"][0]
    assert deployment["metadata"]["labels"] == LABEL  # so down finds it


def test_no_other_manifest_of_this_cluster_has_a_hostpath():
    found = [str(path.relative_to(ROOT))
             for folder in (HERE, HERE.parent / "kubernetes", ROOT / "demo-states" / "catalog")
             for path in folder.rglob("*.y*ml") if "hostPath" in path.read_text()]
    assert found == ["scripts/demos/can_i/checks/hostpath.yaml"]


def test_staging_holds_the_role_production_starts_with_and_the_group_is_bound_in_both():
    docs = documents(CLUSTER / "developer.yaml")
    role = next(d for d in docs if d["kind"] == "Role")
    assert role["metadata"]["namespace"] == "staging"
    assert role["rules"] == yaml.safe_load((TAKE / "before.yaml").read_text())["rules"]
    bindings = [d for d in docs if d["kind"] == "RoleBinding"]
    assert sorted(b["metadata"]["namespace"] for b in bindings) == ["production", "staging"]
    for binding in bindings:
        assert binding["metadata"]["name"] == "developer"
        assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "developer"}
        assert binding["subjects"] == [{"kind": "Group", "name": "release-demo:developers",
                                        "apiGroup": "rbac.authorization.k8s.io"}]
    assert len(docs) == 3


def test_every_object_in_cluster_has_the_label_and_stays_in_the_two_namespaces():
    docs = [d for path in CLUSTER.iterdir() for d in documents(path)]
    assert len(docs) == 5
    for doc in docs:
        assert doc["metadata"]["labels"] == LABEL
        assert doc["metadata"]["namespace"] in ("staging", "production")
        assert doc["kind"] in ("Role", "RoleBinding", "Secret")


def test_each_namespace_holds_its_own_fake_value_and_nothing_else():
    secrets = {d["metadata"]["namespace"]: d for d in documents(CLUSTER / "secrets.yaml")}
    assert secrets["production"]["stringData"] == {"password": "demo-value-not-a-real-password"}
    assert secrets["staging"]["stringData"] == {"password": "staging-only-demo-value"}
    assert all(d["metadata"]["name"] == "catalog-db" and "data" not in d for d in secrets.values())


def test_sam_cant_hold_break_glass():
    """break_glass.py reads people.yaml's kubernetes_user as may hold break-glass."""
    assert "kubernetes_user" not in yaml.safe_load((ROOT / "people.yaml").read_text())["sam"]
