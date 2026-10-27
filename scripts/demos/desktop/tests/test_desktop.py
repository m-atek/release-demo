"""Demo 6's stage scripts: the combined package, the merged test reports, the
Mac build's helpers, and deploy and check against the Jamf stand-in running
on this machine, with JCR, ServiceNow and the Windows instance left out.

    .venv/bin/python -m pytest -q scripts/demos/desktop/tests
"""

import hashlib
import io
import json
import os
import sys
import threading
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "standins"))
import combine  # noqa: E402
import desktop  # noqa: E402
import jamf  # noqa: E402
import mac_build  # noqa: E402
import reports  # noqa: E402
import run_state  # noqa: E402

sys.path.insert(0, str(HERE.parent.parent / "standins" / "tests"))
from test_jamf import fake_pkg  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Run state and dist/ in a temporary folder."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    for module in (combine, reports, desktop):
        monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("BUILD_URL", raising=False)
    return tmp_path


def config(tracks=("mac", "windows")):
    entry = desktop.service("store-assistant")
    entry["desktop"]["tracks"] = list(tracks)
    return entry


def part(tmp_path, track, version, name, content=b"installer", bom=False, **extra):
    directory = tmp_path / "dist" / "store-assistant" / "parts" / track
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)
    kind = "pkg" if track == "mac" else "msi"
    record = {"version": version, kind: {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "sha512": "x"}, **extra}
    text = json.dumps(record)
    (directory / f"{track}-build.json").write_text(("﻿" if bom else "") + text, encoding="utf-8")
    return directory


MAC = {"app_signature": {"authority": "Developer ID Application: Stand-in Developer (STANDIN01)"},
       "notarization": {"status": "Accepted"}, "gatekeeper": {"accepted": True}, "standin": True}
WINDOWS = {"signature": {"signer": "CN=Expeditious Software (test certificate)", "status": "Valid", "test_certificate": True},
           "standin": True}


def test_combine_makes_one_package_with_both_installers(tmp_path, monkeypatch):
    monkeypatch.setattr(combine, "service", lambda name: config())
    monkeypatch.setattr(combine, "version_of", lambda directory: "1.7.0")
    part(tmp_path, "mac", "1.7.0", "StoreAssistant-1.7.0.pkg", **MAC)
    part(tmp_path, "windows", "1.7.0", "StoreAssistant-1.7.0.msi", bom=True, **WINDOWS)
    monkeypatch.setattr(sys, "argv", ["combine.py", "store-assistant", "--jira-key", "ORD-71"])
    combine.main()
    out = tmp_path / "dist" / "store-assistant"
    info = json.loads((out / "build-info.json").read_text())
    package = (out / "store-assistant-1.7.0.zip").read_bytes()
    assert info["sha256"] == hashlib.sha256(package).hexdigest()
    assert set(info["artifacts"]) == {"pkg", "msi"}
    assert info["artifacts"]["msi"]["test_certificate"] is True
    names = zipfile.ZipFile(io.BytesIO(package)).namelist()
    assert {"mac/StoreAssistant-1.7.0.pkg", "windows/StoreAssistant-1.7.0.msi", "windows/windows-build.json"} <= set(names)


def test_combine_refuses_an_installer_that_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(combine, "service", lambda name: config(["mac"]))
    monkeypatch.setattr(combine, "version_of", lambda directory: "1.7.0")
    directory = part(tmp_path, "mac", "1.7.0", "StoreAssistant-1.7.0.pkg", **MAC)
    (directory / "StoreAssistant-1.7.0.pkg").write_bytes(b"changed")
    monkeypatch.setattr(sys, "argv", ["combine.py", "store-assistant"])
    with pytest.raises(SystemExit):
        combine.main()


def test_combine_refuses_another_version(tmp_path, monkeypatch):
    monkeypatch.setattr(combine, "service", lambda name: config(["mac"]))
    monkeypatch.setattr(combine, "version_of", lambda directory: "1.7.1")
    part(tmp_path, "mac", "1.7.0", "StoreAssistant-1.7.0.pkg", **MAC)
    monkeypatch.setattr(sys, "argv", ["combine.py", "store-assistant"])
    with pytest.raises(SystemExit):
        combine.main()


TRX = """<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>
<UnitTestResult testName="ReadsEveryOrder" outcome="Passed" duration="00:00:00.0100000"/>
<UnitTestResult testName="SkipsAnOrder" outcome="{outcome}" duration="00:00:00.0020000">
<Output><ErrorInfo><Message>Assert.Equal() Failure</Message><StackTrace>at OrdersTests</StackTrace></ErrorInfo></Output>
</UnitTestResult></Results></TestRun>"""


def write_reports(tmp_path, outcome):
    mac = tmp_path / "dist" / "store-assistant" / "parts" / "mac" / "reports"
    windows = tmp_path / "dist" / "store-assistant" / "parts" / "windows" / "reports"
    mac.mkdir(parents=True)
    windows.mkdir(parents=True)
    mac_build.write_junit(mac / "junit-mac.xml", "StoreAssistant (macOS)",
                          [{"name": "decodes", "result": "passed", "seconds": 0.01}])
    (mac / "coverage-mac.xml").write_text('<coverage version="1"/>')
    (windows / "windows.trx").write_text(TRX.format(outcome=outcome))


def test_reports_merges_both_tracks(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "service", lambda name: config())
    write_reports(tmp_path, "Passed")
    monkeypatch.setattr(sys, "argv", ["reports.py", "store-assistant"])
    reports.main()
    merged = (tmp_path / ".release" / "reports" / "junit.xml").read_text()
    assert merged.count("<testcase") == 3 and "<failure" not in merged
    assert (tmp_path / ".release" / "reports" / "coverage-mac.xml").exists()


def test_reports_fails_on_a_failed_windows_test(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "service", lambda name: config())
    write_reports(tmp_path, "Failed")
    monkeypatch.setattr(sys, "argv", ["reports.py", "store-assistant"])
    with pytest.raises(SystemExit):
        reports.main()
    assert "Assert.Equal() Failure" in (tmp_path / ".release" / "reports" / "junit.xml").read_text()


def test_swift_testing_output_becomes_test_cases():
    output = """◇ Test decodes() started.
✔ Test decodes() passed after 0.001 seconds.
✘ Test readsTotals() recorded an issue at OrdersTests.swift:12:5: Expectation failed: (day.count → 3) == (day.orders.count → 0)
✘ Test readsTotals() failed after 0.002 seconds with 1 issue."""
    cases = {c["name"]: c for c in mac_build.swift_testing_cases(output)}
    assert cases["decodes"]["result"] == "passed"
    assert cases["readsTotals"]["result"] == "failed" and "Expectation failed" in cases["readsTotals"]["message"]


def test_the_mac_agent_reads_standin_without_pyyaml(monkeypatch):
    monkeypatch.delenv("STANDIN", raising=False)
    monkeypatch.delenv("STANDIN_APPLE", raising=False)
    assert mac_build.setting("store-assistant", "STANDIN_NOTARY_SECONDS", "x").isdigit()
    assert mac_build.standin_mode("store-assistant") is True
    monkeypatch.setenv("STANDIN_APPLE", "0")
    assert mac_build.standin_mode("store-assistant") is False


# Deploy and check, against the stand-in on this machine

@pytest.fixture
def jamf_client(tmp_path, monkeypatch):
    os.environ["JAMF_STANDIN_QUIET"] = "1"
    httpd = jamf.make_server(tmp_path / "jamf", 0, "client", "secret", "key", "invite", host="127.0.0.1")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = jamf.Jamf(f"http://127.0.0.1:{httpd.server_address[1]}", "client", "secret", standin=True)
    client.create_smart_computer_group("Pilot", [("Computer Name", "like", "pilot")])
    client.create_smart_computer_group("All managed Macs", [])
    client.create_policy("Store Assistant", ["Pilot"])
    on = [{"name": "Store Assistant", "version": "1.6.0", "bundleId": "nl.es.StoreAssistant",
           "path": "/Applications/Store Assistant.app"}]
    client.add_simulated_device("computer", "pilot-sim-01", on)
    client.add_simulated_device("computer", "store-mac-02", on)
    monkeypatch.setattr(desktop, "jamf", lambda cfg: client)
    yield client
    httpd.shutdown()


def zip_with(pkg):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(f"mac/{pkg.name}", pkg.read_bytes())
    return data.getvalue()


def load(name):
    """The demo's own deploy.py or check.py: plain imports find the shared scripts of the same name."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"desktop_{name}", HERE.parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(module, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", [module.__name__ + ".py", "store-assistant", *args])
    return module.main()


def test_pilot_rollout_and_rollback(tmp_path, jamf_client, monkeypatch):
    check, deploy = load("check"), load("deploy")

    monkeypatch.setattr(deploy, "service", lambda name: config(["mac"]))
    monkeypatch.setattr(check, "service", lambda name: config(["mac"]))
    packages = {v: zip_with(fake_pkg(tmp_path, v)) for v in ("1.6.0", "1.7.0")}
    jamf_client.set_policy_package(jamf_client.policy("Store Assistant")["id"],
                                   desktop.ensure_jamf_package(jamf_client, config(), "1.6.0", "StoreAssistant-1.6.0.pkg",
                                                               desktop.member(packages["1.6.0"], ".pkg")[1])["id"])
    monkeypatch.setattr(desktop, "verified_package", lambda state: packages[state["version"]])
    monkeypatch.setattr(desktop, "released_versions", lambda name: ["1.6.0"])
    run_state.save({"service": "store-assistant", "version": "1.7.0", "jira_key": "ORD-71",
                    "build": {"number": "12"}, "started_at": run_state.now()})

    run(deploy, monkeypatch, "staging", "--no-change")
    policy = jamf_client.policy("Store Assistant")
    assert [p["name"] for p in policy["packages"]] == ["StoreAssistant-1.7.0.pkg"] and policy["scope"]["groups"] == ["Pilot"]
    state = run_state.load()
    assert state["deploys"]["staging"]["previous_version"] == "1.6.0"
    assert "stays on the Pilot group" in state["finish_line"]
    run(check, monkeypatch, "staging", "--version", "1.7.0")
    versions = {r["name"]: r["version"] for r in jamf_client.app_versions("All managed Macs", "nl.es.StoreAssistant")}
    assert versions == {"pilot-sim-01": "1.7.0", "store-mac-02": "1.6.0"}

    # A failed production check for the wrong version.
    with pytest.raises(SystemExit):
        run(check, monkeypatch, "production", "--version", "1.7.0")

    run(deploy, monkeypatch, "production")
    assert jamf_client.policy("Store Assistant")["scope"]["groups"] == ["All managed Macs"]
    run(check, monkeypatch, "production", "--version", "1.7.0")

    # /rollback: the policy back on the release before, the scope as it was.
    run(deploy, monkeypatch, "production", "--previous")
    production = run_state.load()["deploys"]["production"]
    assert (production["previous_version"], production["rolled_back_to"]) == ("1.7.0", "1.6.0")
    assert [p["name"] for p in jamf_client.policy("Store Assistant")["packages"]] == ["StoreAssistant-1.6.0.pkg"]
    run(check, monkeypatch, "production", "--version", "1.6.0")


def test_a_rejected_rollout_keeps_the_pilot_scope_and_rolls_back(tmp_path, jamf_client, monkeypatch):
    check, deploy = load("check"), load("deploy")

    monkeypatch.setattr(deploy, "service", lambda name: config(["mac"]))
    monkeypatch.setattr(check, "service", lambda name: config(["mac"]))
    packages = {v: zip_with(fake_pkg(tmp_path, v)) for v in ("1.6.0", "1.7.0")}
    monkeypatch.setattr(desktop, "verified_package", lambda state: packages[state["version"]])
    monkeypatch.setattr(desktop, "released_versions", lambda name: ["1.6.0"])
    monkeypatch.setattr(desktop, "release_package", lambda name, version: packages[version])
    run_state.save({"service": "store-assistant", "version": "1.7.0", "jira_key": "ORD-71",
                    "build": {"number": "13"}, "started_at": run_state.now()})
    run(deploy, monkeypatch, "staging", "--no-change")
    # The change is rejected: production never runs. /rollback starts a new run state.
    run_state.save({"service": "store-assistant", "version": "", "jira_key": "", "build": {"number": "5"},
                    "started_at": run_state.now()})
    run(deploy, monkeypatch, "production", "--previous")
    policy = jamf_client.policy("Store Assistant")
    assert policy["scope"]["groups"] == ["Pilot"]
    assert [p["name"] for p in policy["packages"]] == ["StoreAssistant-1.6.0.pkg"]
    run(check, monkeypatch, "production", "--version", "1.6.0")
