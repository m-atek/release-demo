"""Video 26's additions to video 8's cluster: the audit log, Sam's certificate and Pod Security.
host/k3s.py runs on the tools host as root, so its parts are tested one by one."""

import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HOST = Path(__file__).resolve().parents[1] / "host"
SINCE = "2026-11-02T13:21:50Z"
GROUP = "release-demo:developers"
PROD_VALUE = "demo-value-not-a-real-password"


def load_k3s():
    """k3s.py puts scripts/ first on the path; this folder's check and deploy must stay first."""
    path = list(sys.path)
    spec = importlib.util.spec_from_file_location("k3s", HOST / "k3s.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path[:] = path
    return module


k3s = load_k3s()
POLICY = yaml.safe_load((HOST / "audit-policy.yaml").read_text())


def first_match(verb, resource, namespace, group="", subresource=None):
    """The audit policy's level for a request: the first rule that matches wins, as the API server reads it."""
    wanted = f"{resource}/{subresource}" if subresource else resource
    for rule in POLICY["rules"]:
        if "verbs" in rule and verb not in rule["verbs"]:
            continue
        if "namespaces" in rule and namespace not in rule["namespaces"]:
            continue
        if "resources" in rule and not any(group == entry["group"] and wanted in entry["resources"]
                                           for entry in rule["resources"]):
            continue
        return rule["level"]
    return "None"


@pytest.mark.parametrize("verb", ["get", "list", "watch", "create", "update", "patch", "delete"])
@pytest.mark.parametrize("namespace", ["staging", "production"])
def test_the_first_matching_rule_for_a_secret_is_metadata(verb, namespace):
    assert first_match(verb, "secrets", namespace) == "Metadata"


def test_the_very_first_rule_is_secrets_at_metadata():
    first = POLICY["rules"][0]
    assert first["level"] == "Metadata" and first["resources"] == [{"group": "", "resources": ["secrets"]}]
    assert "verbs" not in first  # every verb: who asked and who got one


def test_the_rule_that_records_bodies_never_covers_a_secret():
    for rule in POLICY["rules"]:
        if rule["level"] in ("Request", "RequestResponse"):
            assert not any("secrets" in entry["resources"] for entry in rule["resources"])


@pytest.mark.parametrize("request_, level", [
    (("create", "deployments", "production", "apps"), "Request"),
    (("patch", "deployments", "staging", "apps"), "Request"),
    (("patch", "deployments", "production", "apps", "scale"), "Request"),
    (("get", "deployments", "production", "apps"), "None"),
    (("create", "pods", "production"), "RequestResponse"),  # a made pod's name is only in the response
    (("delete", "pods", "production"), "Metadata"),
    (("get", "pods", "production"), "None"),
    (("list", "pods", "production"), "None"),
    (("get", "pods", "production", "", "log"), "Metadata"),
    (("create", "pods", "production", "", "exec"), "Metadata"),
    (("create", "pods", "production", "", "attach"), "Metadata"),
    (("create", "pods", "production", "", "portforward"), "Metadata"),
    (("patch", "pods", "production", "", "ephemeralcontainers"), "Metadata"),
    (("create", "roles", "production", "rbac.authorization.k8s.io"), "Metadata"),
    (("update", "clusterrolebindings", None, "rbac.authorization.k8s.io"), "Metadata"),
    (("get", "secrets", "jenkins"), "None"),
    (("create", "pods", "monitoring"), "None"),
    (("create", "selfsubjectaccessreviews", None, "authorization.k8s.io"), "None"),
])
def test_what_the_log_keeps(request_, level):
    assert first_match(*request_) == level


def test_the_policy_omits_the_first_stage_and_ends_with_nothing():
    assert POLICY["omitStages"] == ["RequestReceived"] and POLICY["rules"][-1] == {"level": "None"}


def test_k3s_config_holds_the_audit_flags(monkeypatch):
    monkeypatch.setattr(k3s, "memory_total_mib", lambda: 16000)
    args = k3s.k3s_config()["kube-apiserver-arg"]
    assert args == ["audit-policy-file=/data/k3s/server/audit.yaml", "audit-log-path=/data/k3s/server/logs/audit.log",
                    "audit-log-format=json", "audit-log-maxsize=50", "audit-log-maxbackup=4"]


def test_k3s_config_keeps_what_video_8_set(monkeypatch):
    monkeypatch.setattr(k3s, "memory_total_mib", lambda: 16000)
    config = k3s.k3s_config()
    assert config["node-name"] == "tools-host" and config["data-dir"] == "/data/k3s"
    assert set(config["disable"]) == {"traefik", "servicelb", "metrics-server", "local-storage"}
    assert "max-pods=40" in config["kubelet-arg"]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(k3s, "memory_total_mib", lambda: 16000)
    monkeypatch.setattr(k3s, "CONFIG_DIR", tmp_path / "etc")
    monkeypatch.setattr(k3s, "DROP_IN", tmp_path / "drop-in.conf")
    monkeypatch.setattr(k3s, "AUDIT_POLICY", tmp_path / "server" / "audit.yaml")
    monkeypatch.setattr(k3s, "AUDIT_LOG", tmp_path / "server" / "logs" / "audit.log")
    monkeypatch.setattr(k3s, "pull_token", lambda: "token-for-the-test")
    monkeypatch.setattr(k3s, "run", lambda *command, **options: "")
    return tmp_path


def test_configure_writes_the_policy_and_restarts_k3s_once(configured):
    assert k3s.configure(True) is True  # the first up after this: k3s restarts once, and the pods keep running
    assert k3s.AUDIT_POLICY.read_text() == (HOST / "audit-policy.yaml").read_text()
    assert oct(k3s.AUDIT_POLICY.stat().st_mode)[-3:] == "600" and k3s.AUDIT_LOG.parent.is_dir()
    assert k3s.configure(True) is False  # nothing changed, no second restart


def test_a_changed_policy_alone_restarts_k3s_with_a_fresh_pull_token(configured):
    k3s.configure(True)
    (configured / "etc" / "registries.yaml").unlink()
    k3s.AUDIT_POLICY.write_text("an older policy")
    assert k3s.configure(True) is True
    assert (configured / "etc" / "registries.yaml").exists()  # a restart reads it again: not an expired token


def csr(tmp_path, subject):
    key, out = tmp_path / "key", tmp_path / "csr"
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(out),
                    "-subj", subject], check=True, capture_output=True)
    return base64.b64encode(out.read_bytes()).decode()


@pytest.fixture
def signing(monkeypatch):
    """The cluster's side of a signature, stubbed; the subject check runs real openssl."""
    made = []

    def kubectl(*args, check=True, stdin=None):
        made.append((args, stdin))
        if args[:2] == ("get", "csr"):
            return "Q0VSVA=="
        if args[:2] == ("config", "view"):
            return "Q0E="
        return ""

    monkeypatch.setattr(k3s, "kubectl", kubectl)
    return made


def test_developer_signs_sams_request_for_eight_hours(tmp_path, signing, capsys):
    k3s.developer(csr(tmp_path, f"/O={GROUP}/CN=sam"))
    request = json.loads(signing[0][1])
    assert request["spec"]["expirationSeconds"] == 8 * 3600
    assert request["spec"]["signerName"] == "kubernetes.io/kube-apiserver-client"
    assert request["metadata"]["name"].startswith("release-demo-developer-")
    assert json.loads(capsys.readouterr().out) == {"certificate": "Q0VSVA==", "ca": "Q0E="}


@pytest.mark.parametrize("subject", [
    "/O=release-demo:presenters/CN=sam",
    "/O=system:masters/CN=sam",
    "/CN=sam",
    "/O=release-demo:developers/O=system:masters/CN=sam",
    "/O=system:masters/O=release-demo:developers/CN=sam",
    "/O=release-demo:developers+OU=x/O=system:masters/CN=sam",
    "/O=release-demo:developers,O=system:masters/CN=sam",
    "/O=release-demo:developers/CN=system:admin",
    "/O=release-demo:developers",
])
def test_developer_refuses_any_other_group_or_a_name_the_cluster_keeps(tmp_path, signing, subject):
    with pytest.raises(SystemExit, match="Not a developer's request"):
        k3s.developer(csr(tmp_path, subject))
    assert signing == []  # nothing reached the cluster


def test_presenter_signs_as_it_always_did(tmp_path, signing, capsys):
    k3s.presenter(csr(tmp_path, "/O=release-demo:presenters/CN=presenter"))
    request = json.loads(signing[0][1])
    assert request["spec"]["expirationSeconds"] == 8 * 3600
    assert request["metadata"]["name"].startswith("release-demo-presenter-")
    assert json.loads(capsys.readouterr().out)["ca"] == "Q0E="


def test_the_subject_reader_keeps_an_escaped_value_whole():
    assert k3s.subject_fields("subject=CN=sam,O=release-demo:developers\n") == [("CN", "sam"), ("O", GROUP)]
    assert k3s.subject_fields("subject=O=a\\,O=b") == [("O", "a\\,O=b")]


def event(user, verb, resource, name=None, code=200, at="2026-11-02T13:22:04.123456Z", namespace="production",
          subresource=None, stage="ResponseComplete", **extra):
    reference = {"resource": resource, "namespace": namespace, "apiVersion": "v1"}
    if name:
        reference["name"] = name
    if subresource:
        reference["subresource"] = subresource
    return json.dumps({"kind": "Event", "apiVersion": "audit.k8s.io/v1", "level": "Metadata", "auditID": "a-b-c",
                       "stage": stage, "requestURI": "/api/v1/somewhere", "verb": verb,
                       "user": {"username": user, "groups": ["system:authenticated"]},
                       "sourceIPs": ["10.0.0.1"], "userAgent": "kubectl/v1.34.1", "objectRef": reference,
                       "responseStatus": {"metadata": {}, "code": code}, "requestReceivedTimestamp": at,
                       "stageTimestamp": at, "annotations": {"authorization.k8s.io/decision": "allow"}, **extra}) + "\n"


CONTROLLER = "system:serviceaccount:kube-system:replicaset-controller"
MADE_POD = {"level": "RequestResponse",
            "requestObject": {"metadata": {"generateName": "peek-5b7c9d6f4-"}, "spec": {"containers": [{"env": [
                {"name": "CATALOG", "value": "request-body"}]}]}},
            "responseObject": {"kind": "Pod", "metadata": {"name": "peek-5b7c9d6f4-x2x9q"},
                               "spec": {"volumes": [{"secret": {"secretName": "catalog-db"}}], "note": "response-body"}}}
TAKE = [event("sam", "get", "secrets", "catalog-db", code=403),
        event("sam", "create", "deployments", "peek", code=201, level="Request",
              requestObject={"spec": {"note": "request-body"}}),
        event(CONTROLLER, "create", "pods", code=201, **MADE_POD),
        event("system:node:tools-host", "watch", "secrets", "catalog-db", stage="ResponseStarted"),
        event("sam", "get", "pods", "peek-5b7c9d6f4-x2x9q", subresource="log")]


def rows(lines, name="peek", namespace="production", since=SINCE):
    return list(k3s.audit_rows(lines, k3s.moment(since), namespace, name))


def test_audit_finds_the_takes_five_events():
    found = rows(TAKE)
    assert [(r["code"], r["verb"], r["resource"], r["name"], r["subresource"], r["user"]) for r in found] == [
        (403, "get", "secrets", "catalog-db", None, "sam"),
        (201, "create", "deployments", "peek", None, "sam"),
        (201, "create", "pods", "peek-5b7c9d6f4-x2x9q", None, CONTROLLER),
        (200, "watch", "secrets", "catalog-db", None, "system:node:tools-host"),
        (200, "get", "pods", "peek-5b7c9d6f4-x2x9q", "log", "sam")]


def test_audit_prints_only_these_fields_and_no_body():
    fields = {"time", "code", "verb", "resource", "subresource", "name", "namespace", "user"}
    for row in rows(TAKE):
        assert set(row) == fields
    printed = json.dumps(rows(TAKE))
    for never in ("requestObject", "responseObject", "request-body", "response-body", "generateName", "sourceIPs",
                  "userAgent", "annotations", "10.0.0.1", "stage"):
        assert never not in printed


def test_a_pod_made_with_generate_name_has_its_name_only_in_the_response():
    """v1.36's API server fills objectRef.name from the request's name, which generateName leaves empty."""
    assert "name" not in json.loads(TAKE[2])["objectRef"]
    assert rows([TAKE[2]])[0]["name"] == "peek-5b7c9d6f4-x2x9q"


def test_a_refused_pod_has_no_name_and_doesnt_match():
    refused = event(CONTROLLER, "create", "pods", code=403, responseObject={"kind": "Status", "metadata": {}})
    assert rows([refused]) == []


def test_audit_matches_a_name_that_equals_or_starts_with_it():
    lines = [event("a", "patch", "deployments", "catalog"), event("a", "create", "pods", "catalog-1"),
             event("a", "patch", "deployments", "other"), event("a", "patch", "deployments", "my-catalog"),
             event("a", "get", "secrets", "catalog-db")]
    assert [r["name"] for r in rows(lines, "catalog")] == ["catalog", "catalog-1", "catalog-db"]


def test_only_peek_also_brings_the_secret_it_mounts():
    secret = event("sam", "get", "secrets", "catalog-db", code=403)
    assert rows([secret], "peek") and not rows([secret], "other")
    assert not rows([event("sam", "get", "configmaps", "catalog-db")], "peek")


def test_audit_keeps_to_the_namespace_and_the_time():
    assert rows([event("a", "get", "secrets", "catalog-db", namespace="staging")]) == []
    assert rows([event("a", "get", "secrets", "catalog-db", at="2026-11-02T13:21:49.999999Z")]) == []
    assert rows([event("a", "get", "secrets", "catalog-db", at="2026-11-02T13:21:50.000000Z")])


def test_a_server_dry_run_isnt_an_event():
    """kubectl's --dry-run=server is audited like a write, and writes nothing (video 19's recorder, 2 November)."""
    dry = event("sam", "create", "deployments", "peek", code=201,
                requestURI="/apis/apps/v1/namespaces/production/deployments?dryRun=All&fieldManager=kubectl-create")
    assert rows([dry]) == []
    assert len(rows([event("sam", "create", "deployments", "peek", code=201)])) == 1


def test_a_half_written_last_line_is_skipped():
    assert len(rows([TAKE[0], '{"kind":"Event","objectRef":{"name":"peek'])) == 1


def test_audit_reads_the_log_and_its_rotated_copies_oldest_first(tmp_path, monkeypatch, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(k3s, "AUDIT_LOG", logs / "audit.log")
    (logs / "audit-2026-11-02T13-30-00.000.log").write_text(TAKE[0])
    (logs / "audit.log").write_text(TAKE[1])
    (logs / "other.txt").write_text(TAKE[4])
    os.utime(logs / "audit-2026-11-02T13-30-00.000.log", (1793000000, 1793000000))
    os.utime(logs / "audit.log", (1793000100, 1793000100))
    k3s.audit(["--since", "2026-10-01T00:00:00Z", "--namespace", "production", "--name", "peek"])
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["code"] for row in printed] == [403, 201]


def test_a_rotated_log_older_than_the_mark_isnt_read(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(k3s, "AUDIT_LOG", logs / "audit.log")
    old, new = logs / "audit-old.log", logs / "audit.log"
    old.write_text("x")
    new.write_text("x")
    mark = k3s.moment(SINCE).timestamp()
    os.utime(old, (mark - 1, mark - 1))
    os.utime(new, (mark + 1, mark + 1))
    assert k3s.audit_files(k3s.moment(SINCE)) == [new]


def test_cluster_yaml_labels_the_two_namespaces_only():
    docs = [d for d in yaml.safe_load_all((HOST / "cluster.yaml").read_text()) if d]
    labels = {d["metadata"]["name"]: d["metadata"].get("labels", {}) for d in docs if d["kind"] == "Namespace"}
    enforce = {"pod-security.kubernetes.io/enforce": "baseline", "pod-security.kubernetes.io/enforce-version": "v1.36"}
    assert labels == {"staging": enforce, "production": enforce, "jenkins": {}, "monitoring": {}}


def test_k3ss_version_is_still_the_labels_version():
    boot = (HOST.parents[3] / "tools-host" / "boot.sh").read_text()
    assert "K3S_VERSION=v1.36." in boot


def test_the_fake_values_are_never_in_the_cluster_files_video_8_applies():
    assert PROD_VALUE not in (HOST / "cluster.yaml").read_text()
    assert PROD_VALUE not in (HOST / "audit-policy.yaml").read_text()
