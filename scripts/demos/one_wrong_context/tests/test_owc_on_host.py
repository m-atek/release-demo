"""The admin's steps: what's deleted, by name; what's only read; what reads as a refusal."""

import json
import os
from pathlib import Path

import pytest
import yaml

import on_host

ROOT = Path(__file__).resolve().parents[4]


class Kubectl:
    """on_host.kubectl: records each call and answers from a small cluster."""

    def __init__(self):
        self.calls = []
        self.policy = self.binding = True
        self.replicas = {"staging": (3, 3), "production": (0, 0)}
        self.others = []
        self.forecast = []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[:2] == ("get", "validatingadmissionpolicy"):
            return "validatingadmissionpolicy.admissionregistration.k8s.io/no-zero\n" if self.policy else ""
        if args[:2] == ("get", "validatingadmissionpolicybinding"):
            return "validatingadmissionpolicybinding.admissionregistration.k8s.io/no-zero\n" if self.binding else ""
        if args[:2] == ("get", "deployment"):
            namespace = args[args.index("-n") + 1]
            if namespace not in self.replicas:
                return ""
            wanted, ready = self.replicas[namespace]
            return json.dumps({"spec": {"replicas": wanted}, "status": {"readyReplicas": ready} if ready else {}})
        if args[0] == "get" and args[1] == on_host.WORKLOADS:
            return "\n".join(["deployment.apps/catalog", *self.others])
        if args[:2] == ("get", "pods"):
            return "\n".join(self.forecast)
        if args[0] == "delete":
            if args[1] == "validatingadmissionpolicybinding":
                self.binding = False
            else:
                self.policy = False
            return ""
        if args[0] == "scale":
            namespace = args[args.index("-n") + 1]
            self.replicas[namespace] = (3, 0)
            return ""
        raise AssertionError(f"not expected: {args}")


@pytest.fixture
def cluster(monkeypatch):
    fake = Kubectl()
    monkeypatch.setattr(on_host, "kubectl", fake)
    monkeypatch.setattr(on_host, "active", lambda: True)
    return fake


def writes(fake):
    return [call for call in fake.calls if call[0] not in ("get",)]


def test_the_counts_are_catalogs_in_services_yaml():
    config = yaml.safe_load((ROOT / "services.yaml").read_text())["catalog"]
    assert on_host.REPLICAS == {env["namespace"]: env["replicas"] for env in config["environments"].values()}
    assert on_host.DEPLOYMENT == config["kubernetes"]["release"]


def test_it_deletes_two_objects_by_name_the_binding_first(cluster, capsys):
    on_host.remove()
    deletes = [call for call in cluster.calls if call[0] == "delete"]
    assert deletes == [("delete", "validatingadmissionpolicybinding", "no-zero", "--ignore-not-found"),
                       ("delete", "validatingadmissionpolicy", "no-zero", "--ignore-not-found")]
    assert "Nothing else touched" in capsys.readouterr().out


def test_it_deletes_no_namespace_pod_or_deployment():
    text = (Path(on_host.__file__)).read_text()
    code = "\n".join(line for line in text.splitlines() if "kubectl(" in line and '"delete"' in line)
    assert code.count('"delete"') == 2
    assert "namespace" not in code and "deployment" not in code and "pod" not in code


def test_a_namespace_left_short_is_scaled_back_and_only_that_one(cluster, capsys):
    on_host.remove()
    scales = [call for call in cluster.calls if call[0] == "scale"]
    assert scales == [("scale", "deployment", "catalog", "-n", "production", "--replicas=3")]
    assert "catalog in production back on 3 pods" in capsys.readouterr().out


def test_nothing_short_nothing_scaled(cluster):
    cluster.replicas["production"] = (3, 3)
    on_host.remove()
    assert not [call for call in cluster.calls if call[0] == "scale"]


def test_a_deployment_that_isnt_there_isnt_short(cluster):
    del cluster.replicas["production"]
    assert on_host.short_of_pods() == {}


def test_with_k3s_down_it_says_the_policy_stays(cluster, capsys, monkeypatch):
    monkeypatch.setattr(on_host, "active", lambda: False)
    on_host.remove()
    assert cluster.calls == []
    assert "stays on its disk" in capsys.readouterr().out


def test_a_second_remove_finds_nothing_and_says_the_same(cluster):
    on_host.remove()
    on_host.remove()
    assert not cluster.policy and not cluster.binding


def test_nothing_in_the_way_means_no_refusal(cluster):
    assert on_host.refusal() is None
    assert not writes(cluster)


def test_it_refuses_while_production_runs_anything_else_and_leaves_it_alone(cluster):
    cluster.others = ["statefulset.apps/catalog-postgres"]
    found = on_host.refusal()
    assert "catalog-postgres" in found and "database-blip" in found
    assert not writes(cluster)


def test_it_refuses_while_forecast_fills_the_node_and_leaves_it_alone(cluster):
    cluster.forecast = ["pod/forecast-abc"]
    found = on_host.refusal()
    assert "forecast" in found and "down hotfix-room" in found
    assert not writes(cluster)


def test_status_changes_nothing_and_reads_as_it_is(cluster, capsys):
    on_host.status()
    out = capsys.readouterr().out
    assert "policy no-zero: on, binding no-zero: on" in out
    assert "catalog in production: 0 wanted, 0 ready" in out
    assert "catalog in staging: 3 wanted, 3 ready" in out
    assert not writes(cluster)


def test_it_only_runs_as_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as stopped:
        on_host.main()
    assert "root" in str(stopped.value)


def test_the_kubeconfig_is_k3s_admins_and_nothing_prints_it():
    assert on_host.ADMIN == "/etc/rancher/k3s/k3s.yaml"
    assert "print(" not in "\n".join(line for line in Path(on_host.__file__).read_text().splitlines() if "ADMIN" in line)
