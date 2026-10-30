import base64
import importlib.util
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parents[1]
VALUES = (HERE.parents[2] / "deploy" / "production" / "catalog" / "values.yaml").read_text()
DIGEST = "sha256:" + "c" * 64


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reset():
    return load("argocd_reset", HERE / "reset.py")


def test_reset_pins_replicas_and_digest_and_keeps_the_comments():
    module = reset()
    five = VALUES.replace("replicas: 3", "replicas: 5")
    pinned = module.pinned_values(five, 3, DIGEST)
    assert yaml.safe_load(pinned)["replicas"] == 3 and yaml.safe_load(pinned)["image"]["digest"] == DIGEST
    assert pinned.count("#") == VALUES.count("#")
    assert module.needs_reset(five, 3, DIGEST) and not module.needs_reset(pinned, 3, DIGEST)
    assert module.pinned_values(pinned, 3, DIGEST) == pinned  # a second run changes nothing


def test_reset_hands_itself_to_the_tools_host_through_the_platforms_host_module():
    module = reset()
    assert Path(module.host.__file__).resolve() == HERE.parents[1] / "host.py"
    assert hasattr(module.host, "hand_over") and hasattr(module.host, "on_host")


def test_a_dry_run_changes_nothing(capsys):
    module = reset()
    done = []
    dry = module.Reset(dry_run=True)
    dry.act("commit the values", lambda: done.append(1))
    assert done == [] and dry.actions == 1 and "would commit the values" in capsys.readouterr().out


def test_k3s_writes_its_audit_log_with_the_policy():
    import cluster

    config = cluster.k3s_config()
    args = config["kube-apiserver-arg"]
    assert f"audit-policy-file={cluster.AUDIT_POLICY}" in args
    assert "audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log" in args
    assert "audit-log-maxsize=50" in args
    assert set(config["disable"]) == {"traefik", "servicelb", "metrics-server", "local-storage"}


def test_k3s_pulls_from_jcr_on_the_tools_hosts_private_address():
    import cluster

    claims = base64.urlsafe_b64encode(json.dumps({"sub": "jfrt@01x/users/inventory-reader"}).encode()).decode()
    token = f"header.{claims.rstrip('=')}.signature"
    found = cluster.registries("http://10.40.0.38:8082", token)
    assert found["mirrors"]["artifactory-demo.es.nl"]["endpoint"] == ["http://10.40.0.38:8082"]
    assert found["configs"]["10.40.0.38:8082"]["auth"] == {"username": "inventory-reader", "password": token}
    assert cluster.token_subject("not-a-jwt") == "inventory-reader"


def test_setup_fills_in_the_repository_and_the_nodes_address(tmp_path, monkeypatch):
    import cluster

    install = tmp_path / "install.yaml"
    install.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: argocd-cm}\n")
    monkeypatch.setattr(cluster.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "ok"})())
    out = tmp_path / "out"
    out.mkdir()
    cluster.render({"repository": "owner/release-demo", "address": "10.40.0.200"}, install, out)
    assert "https://github.com/owner/release-demo.git" in (out / "production.yaml").read_text()
    assert '"10.40.0.200"' in (out / "drift-recorder.yaml").read_text()
    assert "__" not in (out / "production.yaml").read_text() + (out / "drift-recorder.yaml").read_text()


def test_argocds_crds_are_established_before_its_project_and_application_are_applied(monkeypatch):
    import cluster

    rendered = yaml.safe_dump_all([
        {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
         "metadata": {"name": "applications.argoproj.io"}},
        {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "catalog-production"}},
    ])
    calls = []
    monkeypatch.setattr(cluster, "kubectl", lambda *args, **kwargs: calls.append((args, kwargs.get("stdin"))) or "")
    monkeypatch.setattr(cluster, "render", lambda *args: rendered)
    monkeypatch.setattr(cluster, "manifest", lambda: None)
    for name in ("notifications_secret", "recorder_token", "wait", "pipeline_token"):
        monkeypatch.setattr(cluster, name, lambda *args, **kwargs: None)
    cluster.argocd({})
    crds, established, everything = calls[1:4]
    assert [d["kind"] for d in yaml.safe_load_all(crds[1])] == ["CustomResourceDefinition"]
    assert established[0][:2] == ("wait", "--for=condition=Established")
    assert "crd/applications.argoproj.io" in established[0]
    assert everything[1] == rendered


def test_a_new_cluster_restarts_argocd_server_with_admin_on_for_its_first_password(monkeypatch):
    import cluster

    secret = json.dumps({"data": {"password": base64.b64encode(b"first").decode()}})
    calls, stored = [], {}

    def kubectl(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("get", "secret", "argocd-initial-admin-secret"):
            return secret if ("rollout", "restart", "deployment/argocd-server", "-n", "argocd") in calls else ""
        return ""

    def api(method, path, token=None, body=None):
        if path == "/api/v1/session":
            return (200, {"token": "session"}) if body["password"] == "first" else (401, {})
        return 200, {"token": "pipeline"}

    monkeypatch.setattr(cluster, "kubectl", kubectl)
    monkeypatch.setattr(cluster, "api", api)
    monkeypatch.setattr(cluster, "parameter", lambda name: None)
    monkeypatch.setattr(cluster, "wait", lambda what, check, timeout: check())
    monkeypatch.setattr(cluster, "rolled_out", lambda *args: lambda: True)
    monkeypatch.setattr(cluster, "ssm", lambda: type("SSM", (), {"put_parameter": lambda self, **k: stored.update(k)})())
    cluster.pipeline_token()
    assert [json.loads(c[-1])["data"]["admin.enabled"] for c in calls if c[0] == "patch"] == ["true", "false"]
    assert stored["Name"] == "/release-demo/argocd/pipeline-token" and stored["Value"] == "pipeline"


def test_the_pinned_argocd_manifest_is_a_release_at_least_two_weeks_old():
    import cluster

    assert cluster.ARGOCD_VERSION == "v3.5.5" and len(cluster.ARGOCD_SHA256) == 64
    assert cluster.ARGOCD_MANIFEST.endswith(f"/{cluster.ARGOCD_VERSION}/manifests/install.yaml")


def test_idle_counts_connections_to_argocd_only():
    idle = load("cluster_idle", HERE / "host" / "idle.py")
    table = "\n".join([
        "ipv4 2 tcp 6 86399 ESTABLISHED src=10.40.0.38 dst=10.40.0.200 sport=51234 dport=30080 src=10.42.0.9 ...",
        "ipv4 2 tcp 6 117 TIME_WAIT src=10.40.0.38 dst=10.40.0.200 sport=51235 dport=30080 src=10.42.0.9 ...",
        "ipv4 2 tcp 6 86399 ESTABLISHED src=127.0.0.1 dst=127.0.0.1 sport=40000 dport=30080 ...",
        "ipv4 2 tcp 6 86399 ESTABLISHED src=10.42.0.5 dst=10.43.0.1 sport=40000 dport=443 ...",
    ])
    assert idle.argocd_connections(table) == 2


def test_break_glass_kubeconfig_points_at_the_tunnel(tmp_path):
    import break_glass

    key = tmp_path / "pieter-oncall.key"
    key.write_text("KEY")
    config = break_glass.kubeconfig("pieter-oncall", {"certificate": "CERT", "ca": "CA"}, key)
    assert config["clusters"][0]["cluster"]["server"] == "https://127.0.0.1:26443"
    assert config["contexts"][0]["context"]["namespace"] == "production"
    assert config["users"][0]["user"]["client-key-data"] == base64.b64encode(b"KEY").decode()
    assert break_glass.person("pieter")["kubernetes_user"] == "pieter-oncall"


def test_up_prints_only_this_starts_boot_not_the_earlier_ones_in_the_log():
    demo = load("argocd_demo", HERE / "demo.py")
    started = 1793391480  # 2026-10-30T20:18:00Z, the Mac's clock when it started the host
    old = ["== boot 2026-10-30T19:41:44Z", "No argocd-initial-admin-secret: the admin password is unknown",
           "== boot done"]
    new = ["== boot 2026-10-30T20:18:33Z", "k3s v1.37.1+k3s1, Argo CD v3.5.5", "Ready", "== boot done"]
    assert demo.boot_section(old, started) == []  # the host is up, but this boot hasn't written yet
    assert demo.boot_section(new, started) == new
    assert demo.boot_section(new, 0) == new  # a host that ran already: its newest boot
    assert demo.boot_section(["== boot 2026-10-30T20:17:30Z", "Ready"], started) != []  # a minute of skew
    assert demo.boot_section([], started) == [] and demo.boot_section(["  apt output"], 0) == []


def test_status_names_the_cause_argo_cd_wraps_not_its_wrapping():
    import cluster

    message = ("Failed to load target state: failed to generate manifest for source 1 of 1: rpc error: code = "
               "Unknown desc = Manifest generation error (cached): failed to execute helm template command: failed "
               "running helm: `helm template . --name-template catalog --namespace production --kube-version 1.37.1 "
               "<api versions removed> --include-crds` failed exit status 1: Error: execution error at "
               "(catalog/templates/deployment.yaml:39:52): image.digest is required: the image is pinned by digest"
               "\n\nUse --debug flag to render out invalid YAML")
    app = {"status": {"sync": {"status": "Unknown", "revision": "main"}, "health": {"status": "Healthy"},
                      "conditions": [{"type": "ComparisonError", "message": message}]}}
    line = cluster.app_line(app)
    assert line.startswith("catalog-production: Unknown at main, Healthy (execution error at ")
    assert "image.digest is required" in line and "--debug" not in line
    assert cluster.cause("app path does not exist") == "app path does not exist"
    assert cluster.cause("") == ""
