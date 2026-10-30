"""Argo CD's settings, the audit policy and the break-glass role, read as the cluster reads them.
The kustomization renders offline over a small stand-in for Argo CD's install manifest."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

HOST = Path(__file__).resolve().parents[1] / "host"
ARGOCD = HOST / "argocd"


def documents(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text()) if d]


def by_name(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def test_argocd_is_read_only_past_access_with_one_pipeline_account():
    docs = documents(ARGOCD / "settings.yaml")
    cm = by_name(docs, "ConfigMap", "argocd-cm")["data"]
    assert cm["users.anonymous.enabled"] == "true" and cm["admin.enabled"] == "false"
    assert cm["accounts.release-pipeline"] == "apiKey"
    rbac = by_name(docs, "ConfigMap", "argocd-rbac-cm")["data"]
    assert rbac["policy.default"] == "role:readonly"
    policies = [line.split(", ") for line in rbac["policy.csv"].strip().splitlines()]
    assert policies == [["p", "release-pipeline", "applications", "get", "production/catalog-production", "allow"],
                        ["p", "release-pipeline", "applications", "sync", "production/catalog-production", "allow"]]
    assert by_name(docs, "ConfigMap", "argocd-cmd-params-cm")["data"]["server.insecure"] == "true"


def test_every_automated_sync_reaches_the_recorder_once():
    data = by_name(documents(ARGOCD / "settings.yaml"), "ConfigMap", "argocd-notifications-cm")["data"]
    trigger = yaml.safe_load(data["trigger.on-automated-sync"])[0]
    assert trigger["send"] == ["automated-sync"] and trigger["oncePer"] == "app.status.operationState.finishedAt"
    assert "initiatedBy.automated == true" in trigger["when"] and "'Succeeded'" in trigger["when"]
    service = yaml.safe_load(data["service.webhook.drift-recorder"])
    assert service["url"] == "http://drift-recorder.argocd.svc:9095/"
    assert {"name": "Authorization", "value": "Bearer $drift-recorder-token"} in service["headers"]
    app = by_name(documents(ARGOCD / "production.yaml"), "Application", "catalog-production")
    assert "notifications.argoproj.io/subscribe.on-automated-sync.drift-recorder" in app["metadata"]["annotations"]


def test_the_application_syncs_production_from_git_and_heals_itself():
    docs = documents(ARGOCD / "production.yaml")
    app = by_name(docs, "Application", "catalog-production")["spec"]
    assert app["source"]["path"] == "deploy/production/catalog" and app["source"]["targetRevision"] == "main"
    assert app["source"]["helm"]["releaseName"] == "catalog"  # so the deployment is catalog, as kubectl names it
    assert app["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}
    assert app["destination"] == {"server": "https://kubernetes.default.svc", "namespace": "production"}
    project = by_name(docs, "AppProject", "production")["spec"]
    assert project["destinations"] == [{"server": "https://kubernetes.default.svc", "namespace": "production"}]
    assert project["sourceRepos"] == ["__REPOSITORY__"] and project["clusterResourceWhitelist"] == []


def test_the_audit_policy_records_deployment_writes_in_production_and_certificates():
    policy = yaml.safe_load((HOST / "audit-policy.yaml").read_text())
    first, second, last = policy["rules"]
    assert first == {"level": "RequestResponse", "verbs": ["create", "update", "patch", "delete"],
                     "namespaces": ["production"],
                     "resources": [{"group": "apps", "resources": ["deployments", "deployments/scale"]}]}
    assert second["level"] == "Metadata" and last == {"level": "None"}
    assert policy["omitStages"] == ["RequestReceived"]


def test_break_glass_patches_deployments_in_production_and_reads_no_secrets():
    docs = documents(HOST / "break-glass.yaml")
    role = by_name(docs, "Role", "break-glass")
    assert role["metadata"]["namespace"] == "production"
    granted = {(group, resource, verb) for rule in role["rules"] for group in rule["apiGroups"]
               for resource in rule["resources"] for verb in rule["verbs"]}
    assert ("apps", "deployments/scale", "patch") in granted and ("apps", "deployments", "patch") in granted
    assert not any(resource == "secrets" for _, resource, _ in granted)
    assert not any(verb in ("create", "delete", "*") for _, _, verb in granted)
    binding = by_name(docs, "RoleBinding", "break-glass")
    assert binding["subjects"] == [{"kind": "Group", "name": "release-demo:break-glass",
                                    "apiGroup": "rbac.authorization.k8s.io"}]
    assert not any(d["kind"] in ("ClusterRole", "ClusterRoleBinding") for d in docs)


STAND_IN = """
apiVersion: v1
kind: ConfigMap
metadata: {name: argocd-cm, labels: {app.kubernetes.io/part-of: argocd}}
data: {resource.exclusions: "[]"}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: argocd-rbac-cm}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: argocd-cmd-params-cm}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: argocd-notifications-cm}
---
apiVersion: v1
kind: Service
metadata: {name: argocd-server}
spec:
  ports:
    - {name: http, port: 80, protocol: TCP, targetPort: 8080}
    - {name: https, port: 443, protocol: TCP, targetPort: 8080}
  selector: {app.kubernetes.io/name: argocd-server}
"""


def stand_in():
    """Argo CD's install.yaml cut down to the objects the patches touch, plus every one they delete."""
    docs = [STAND_IN.strip()]
    for patch in documents(ARGOCD / "without-dex-and-applicationset.yaml"):
        docs.append(yaml.safe_dump({"apiVersion": patch["apiVersion"], "kind": patch["kind"],
                                    "metadata": {"name": patch["metadata"]["name"]}}))
    return "\n---\n".join(docs)


@pytest.mark.skipif(not shutil.which("kubectl"), reason="needs kubectl, for kubectl kustomize")
def test_the_kustomization_renders_without_dex_and_applicationset(tmp_path):
    for item in ARGOCD.iterdir():
        shutil.copy(item, tmp_path / item.name)
    (tmp_path / "install.yaml").write_text(stand_in())
    rendered = subprocess.run(["kubectl", "kustomize", str(tmp_path)], capture_output=True, text=True, check=True)
    docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
    names = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert not any("dex" in name or "applicationset" in name for _, name in names)
    assert all(d["metadata"].get("namespace") == "argocd" for d in docs if d["kind"] != "ClusterRole")
    server = by_name(docs, "Service", "argocd-server")["spec"]
    assert server["type"] == "NodePort" and server["ports"][0]["nodePort"] == 30080
    cm = by_name(docs, "ConfigMap", "argocd-cm")["data"]
    assert cm["resource.exclusions"] == "[]" and cm["users.anonymous.enabled"] == "true"
    assert {("Application", "catalog-production"), ("AppProject", "production"),
            ("EndpointSlice", "drift-recorder-node"), ("Service", "drift-recorder")} <= names
