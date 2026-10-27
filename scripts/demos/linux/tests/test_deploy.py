"""The deploy against a fake fleet: the cloud first, then the data center, and a
failed check at inv-dc-01 stops the rollout with the lines Slack shows."""

from types import SimpleNamespace

import pytest

import fleet
import report
import run_state
from conftest import HERE, JAVA8, load, result

deploy = load("linux_deploy", HERE / "deploy.py")


@pytest.fixture
def fake_fleet(monkeypatch):
    calls = []

    def cloud(playbook, extra, names, tags=None, prefix=""):
        calls.append(("cloud", extra["rollout_action"], names, extra.get("package", {}).get("url")))
        return 0, [result(n, "ok", "2.8.0") for n in names]

    def dc(playbook, extra, names, tags=None, secrets=None, prefix=""):
        calls.append(("dc", extra["rollout_action"], names, extra.get("package", {}).get("url")))
        return 2, [result("inv-dc-01", "rolled-back", "2.7.3", detail=JAVA8)]

    monkeypatch.setattr(fleet, "run_cloud", cloud)
    monkeypatch.setattr(fleet, "run_dc", dc)
    monkeypatch.setattr(fleet, "tools_host_private_ip", lambda session: "10.40.0.155")
    monkeypatch.setattr(fleet, "dc_mode", lambda: "jenkins")
    monkeypatch.setattr(deploy, "preflight", lambda names: None)
    lines = []
    monkeypatch.setattr(report, "post_line", lines.append)
    return calls, lines


def state():
    run_state.save({"service": "inventory", "version": "2.8.0", "jira_key": "ORD-44", "started_at": 0,
                    "build": {"number": "57", "url": ""}, "stages": {},
                    "package": {"path": "demos-candidates/inventory/2.8.0/57/inventory-2.8.0.zip", "sha256": "a" * 64}})
    return run_state.load()


def test_a_failed_check_stops_the_rollout(fake_fleet):
    calls, lines = fake_fleet
    current = state()
    args = SimpleNamespace(package_file=None, back=False, previous=False)
    with pytest.raises(SystemExit) as stop:
        deploy.deploy(args, current, current.setdefault("deploys", {}), reporting=False)
    assert stop.value.code == 1
    assert [c[:3] for c in calls] == [("cloud", "deploy", ["inv-cloud-01", "inv-cloud-02"]),
                                      ("dc", "deploy", ["inv-dc-01", "inv-dc-02"])]
    # The cloud servers fetch over the VPC, the data center through Cloudflare.
    assert calls[0][3] == "http://10.40.0.155:8082/artifactory/demos-candidates/inventory/2.8.0/57/inventory-2.8.0.zip"
    assert calls[1][3].startswith("https://artifactory-demo.es.nl/artifactory/demos-candidates/inventory/")
    saved = run_state.load()
    production = saved["deploys"]["production"]
    assert (production["number"], production["previous_version"], production["stopped_at"]) == ("2.8.0", "2.7.3", "inv-dc-01")
    assert [s["result"] for s in production["servers"]] == ["ok", "ok", "rolled-back", "not-started"]
    assert saved["finish_line"] == "Rollout stopped at inv-dc-01 after {total}. inv-dc-02 untouched."


def test_the_slack_line_goes_out_when_reporting(fake_fleet, monkeypatch):
    calls, lines = fake_fleet
    for name in ("affected_cis", "note", "events"):
        monkeypatch.setattr(report, name, lambda *a, **k: None)
    current = state()
    with pytest.raises(SystemExit):
        deploy.deploy(SimpleNamespace(package_file=None), current, current.setdefault("deploys", {}), reporting=True)
    assert lines == ["inv-dc-01 failed its check. Rolled back to 2.7.3. Rollout stopped."]


def test_back_returns_only_the_servers_this_run_moved(fake_fleet, monkeypatch):
    calls, _ = fake_fleet
    current = state()
    current["deploys"] = {"production": {"servers": [result("inv-cloud-01", "ok", "2.8.0"),
                                                     result("inv-cloud-02", "ok", "2.8.0"),
                                                     result("inv-dc-01", "rolled-back", "2.7.3")]}}
    monkeypatch.setattr(fleet, "run_cloud", lambda p, e, n, tags=None, prefix="": (
        calls.append(("cloud", e["rollout_action"], n, e["previous"])) or
        (0, [result(x, "ok", "2.7.3", target="2.7.3") for x in n])))
    deploy.back(SimpleNamespace(back=True, previous=False), current, current["deploys"], reporting=False)
    assert calls == [("cloud", "back", ["inv-cloud-01", "inv-cloud-02"], {"inv-cloud-01": "2.7.3", "inv-cloud-02": "2.7.3"})]
    assert run_state.load()["deploys"]["production"]["rolled_back_to"] == "2.7.3"
