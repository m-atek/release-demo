"""kube.py's helpers, and how deploy.py and check.py read the cluster."""

import json

import pytest

import check
import deploy
import kube


def pod(version, ready, digest="sha256:aa", deleting=False):
    meta = {"name": f"catalog-{version}", "labels": {"app.kubernetes.io/version": version}}
    if deleting:
        meta["deletionTimestamp"] = "now"
    return {"metadata": meta, "status": {"containerStatuses": [
        {"ready": ready, "imageID": f"artifactory-demo.es.nl/docker-local/catalog@{digest}"}]}}


class FakeApi:
    def __init__(self, pods, ready):
        self._pods, self._ready = pods, ready

    def pods(self, namespace, release):
        return self._pods

    def deployment(self, namespace, name):
        return {"status": {"readyReplicas": self._ready}}


def test_durations():
    assert kube.parse_duration("3m") == 180
    assert kube.parse_duration("1m30s") == 90
    assert kube.minutes("3m") == "3 minutes"
    assert kube.minutes("1m") == "1 minute"
    assert kube.clock(181) == "3:01"


def test_deployed_is_the_newest_deployed_revision():
    history = [{"revision": 1, "status": "superseded"}, {"revision": 2, "status": "failed"},
               {"revision": 3, "status": "deployed"}]
    assert kube.deployed(history)["revision"] == 3
    assert kube.deployed([]) is None


def test_pod_summary():
    assert kube.pod_summary(pod("1.9.0", False, "sha256:bb")) == ("1.9.0", False, False, "sha256:bb")


def test_check_wants_every_pod_ready_on_the_version_and_digest():
    good = check.cluster_checks(FakeApi([pod("1.9.1", True)] * 3, 3), "production", "catalog", 3, "1.9.1", "sha256:aa")
    assert all(ok for ok, _ in good)
    mixed = check.cluster_checks(FakeApi([pod("1.9.1", True)] * 2 + [pod("1.8.2", True)], 3),
                                 "production", "catalog", 3, "1.9.1", None)
    assert not mixed[0][0]
    other_digest = check.cluster_checks(FakeApi([pod("1.9.1", True, "sha256:cc")] * 3, 3),
                                        "production", "catalog", 3, "1.9.1", "sha256:aa")
    assert not other_digest[1][0]


def test_terminating_pods_dont_count():
    pods = [pod("1.9.1", True)] * 3 + [pod("1.8.2", True, deleting=True)]
    assert all(ok for ok, _ in check.cluster_checks(FakeApi(pods, 3), "production", "catalog", 3, "1.9.1", None))


def test_values_pin_the_digest_and_carry_the_environment():
    config = {"kubernetes": {"image": "artifactory-demo.es.nl/docker-local/catalog"},
              "environments": {"production": {"replicas": 3, "node_port": 30082, "settings": {}}}}
    values = deploy.values(config, "production", {"image": {"digest": "sha256:aa"}})
    assert values["image"] == {"repository": "artifactory-demo.es.nl/docker-local/catalog", "digest": "sha256:aa"}
    assert values["replicas"] == 3 and values["nodePort"] == 30082 and values["environment"] == "production"


def test_back_after_helms_own_rollback_confirms_it(monkeypatch, tmp_path):
    """The pipeline's rollback after Helm already rolled back: no second helm rollback."""
    import contextlib
    import run_state

    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")
    history = [{"revision": 1, "app_version": "1.8.2", "status": "superseded"},
               {"revision": 2, "app_version": "1.9.0", "status": "failed"},
               {"revision": 3, "app_version": "1.8.2", "status": "deployed"}]
    monkeypatch.setattr(kube, "credentials", lambda quiet=False: contextlib.nullcontext(tmp_path / "kubeconfig"))
    monkeypatch.setattr(kube, "history", lambda *args: history)
    monkeypatch.setattr(kube, "helm", lambda *args, **kwargs: pytest.fail("helm rollback ran twice"))
    notes = []
    monkeypatch.setattr(deploy, "note", lambda state, text: notes.append(text))
    state = {"version": "1.9.0", "deploys": {"production": {
        "previous_number": "1", "previous_version": "1.8.2", "helm_rolled_back_to": "1.8.2",
        "helm_rollback_number": "3", "rollback_notes": "Helm rolled back: image sha256:aa."}}}
    config = {"kubernetes": {"release": "catalog"},
              "environments": {"production": {"namespace": "production", "node_port": 30082}}}
    deploy.go_back(config, "production", state, False)
    record = json.loads((tmp_path / "state.json").read_text())["deploys"]["production"]
    assert record["rolled_back_to"] == "1.8.2" and record["rolled_back_by"] == "Helm"
    assert "sha256:aa" in notes[0]


def test_check_fails_at_once_after_helms_rollback_with_the_notes(monkeypatch, tmp_path, capsys):
    import sys
    import run_state

    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")
    run_state.save({"version": "1.9.0", "deploys": {"production": {
        "helm_rolled_back_to": "1.8.2", "helm_rollback_number": "3",
        "helm_rollback_reason": "Not ready after 3 minutes.", "rollback_notes": "Helm rolled back: image sha256:aa."}}})
    monkeypatch.setattr(sys, "argv", ["check.py", "catalog", "production", "--version", "1.9.0",
                                      "--url", "http://127.0.0.1:9"])
    with pytest.raises(SystemExit) as stop:
        check.main()
    assert stop.value.code == 1
    assert "FAIL 1.9.0 isn't live: Not ready after 3 minutes. Helm rolled back to 1.8.2" in capsys.readouterr().out
    assert json.loads((tmp_path / "state.json").read_text())["check"]["notes"] == "Helm rolled back: image sha256:aa."
