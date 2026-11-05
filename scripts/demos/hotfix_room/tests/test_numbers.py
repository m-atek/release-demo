"""The arithmetic the video rests on, from the repository's own files: what the scheduler can hand
out on this host's size, what each pod reserves, and that the hotfix's pod can't fit. It isn't a
measurement: that is rehearse.py. It fails when anyone changes a request or a setting it rests on."""

import importlib.util

import pytest
import yaml

import room
from common import ROOT, service

K3S = ROOT / "scripts/demos/kubernetes/host/k3s.py"
# requests.memory of CoreDNS in k3s's manifests/coredns.yaml at v1.36.5+k3s1, read on 5 November 2026.
COREDNS_MIB = 70


def k3s():
    spec = importlib.util.spec_from_file_location("k3s_settings", K3S)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def kubelet_args(config):
    return dict(argument.split("=", 1) for argument in config["kubelet-arg"])


def mib(text):
    return room.quantity(text) / room.MIB


@pytest.mark.parametrize("mem_total_kib", [16072912, 15900000, 16384000])
def test_the_scheduler_gets_1180_where_the_pods_cgroup_gets_1280(monkeypatch, mem_total_kib):
    """Allocatable = capacity - system-reserved - kube-reserved - the hard eviction threshold; the cgroup leaves
    out the threshold. Kubernetes' docs and pkg/kubelet/cm/node_container_manager_linux.go at v1.36.5 say so."""
    module = k3s()
    monkeypatch.setattr(module, "memory_total_mib", lambda: mem_total_kib // 1024)
    args = kubelet_args(module.k3s_config())
    capacity = mem_total_kib / 1024
    enforced = capacity - mib(args["system-reserved"].split("=")[1]) - mib(args["kube-reserved"].split("=")[1])
    threshold = mib(args["eviction-hard"].split("<")[1])
    assert int(enforced) == module.PODS_MIB == 1280
    assert int(enforced - threshold) == 1180


def request_mib(state, service_name):
    values = yaml.safe_load((ROOT / "demo-states" / service_name / state / "chart" / "values.yaml").read_text())
    return mib(values["resources"]["requests"]["memory"])


def kube_state_metrics_mib():
    documents = yaml.safe_load_all((ROOT / "scripts/demos/kubernetes/host/cluster.yaml").read_text())
    deployment = next(d for d in documents if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "kube-state-metrics")
    return mib(deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"])


def on_the_node(forecast_state):
    catalog = service("catalog")["environments"]
    forecast = service("forecast")["environments"]["production"]
    catalog_pods = sum(environment["replicas"] for environment in catalog.values())
    return (COREDNS_MIB + kube_state_metrics_mib() + catalog_pods * request_mib("1.12.0", "catalog")
            + forecast["replicas"] * request_mib(forecast_state, "forecast"))


def test_the_hotfixs_pod_cant_fit_while_forecast_holds_the_room():
    reserved = on_the_node("2.4.0")
    assert reserved == 1158
    free, ask = 1180 - reserved, request_mib("1.12.1", "catalog")
    assert (free, ask) == (22, 48)
    assert 0 <= free < ask
    assert int(reserved * 100 / 1180) == 98


def test_with_the_memory_given_back_there_is_room_for_the_whole_rollout():
    reserved = on_the_node("2.4.1")
    assert reserved == 534
    assert int(reserved * 100 / 1180) == 45
    assert 1180 - reserved == 646
    assert on_the_node("2.4.0") - reserved == 624
    assert request_mib("1.12.1", "catalog") * 2 <= 646  # a surge pod in each namespace


def test_alloy_would_overfill_the_node_and_the_reset_says_so():
    """On 2 and 5 November k3s.py up printed "Grafana metrics not set up", so no Alloy. With its 96 Mi
    forecast's third pod has no place: the reset's node check catches that before any take."""
    alloy = yaml.safe_load_all((ROOT / "scripts/demos/kubernetes/host/alloy.yaml").read_text())
    deployment = next(d for d in alloy if d and d["kind"] == "Deployment")
    assert mib(deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"]) == 96
    assert on_the_node("2.4.0") + 96 > 1180


def test_the_first_day_check_knows_the_same_figure():
    import rehearse

    assert rehearse.ALLOCATABLE_MIB == 1180


def test_forecasts_real_use_is_what_the_reservation_dwarfs():
    """At 48 Mi a pod the limit stays above a catalog-sized pod's measured peak of 25 MiB (prep 25)."""
    values = yaml.safe_load((ROOT / "demo-states/forecast/2.4.1/chart/values.yaml").read_text())
    assert mib(values["resources"]["requests"]["memory"]) >= 2 * 24
    assert mib(values["resources"]["limits"]["memory"]) >= 2 * 25
