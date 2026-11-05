"""The four demo states against each other: a one-line hotfix, a chart change that gives memory back, and nothing that
changes which stages a release has."""

import difflib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import run_state
from common import ROOT, service

CATALOG, FORECAST = ROOT / "demo-states/catalog", ROOT / "demo-states/forecast"
CODE = ("app.py",)
DIGEST = "sha256:" + "ab" * 32
helm = shutil.which("helm")


def files(folder):
    return {path.relative_to(folder).as_posix(): path for path in folder.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".coverage"}


def changed(before, after):
    old, new = files(before), files(after)
    return sorted(name for name in old.keys() | new.keys() if name not in old or name not in new or old[name].read_bytes() != new[name].read_bytes())


def changed_lines(before, after):
    diff = difflib.ndiff(before.read_text().splitlines(), after.read_text().splitlines())
    return [line for line in diff if line[:2] in ("- ", "+ ")]


def test_the_hotfix_changes_one_line_of_code():
    assert changed(CATALOG / "1.12.0", CATALOG / "1.12.1") == ["VERSION", "app.py", "chart/Chart.yaml", "tests/test_app.py"]
    lines = changed_lines(CATALOG / "1.12.0/app.py", CATALOG / "1.12.1/app.py")
    assert len(lines) == 2
    assert lines[0].startswith("- ") and 'supplier in p["supplier"]' in lines[0]
    assert lines[1].startswith("+ ") and 'folded(supplier) in folded(p["supplier"])' in lines[1]


def test_the_hotfixs_bug_is_real_in_1_12_0_and_gone_in_1_12_1():
    for version, count in (("1.12.0", "0"), ("1.12.1", "4")):
        code = ("import os, sys, json; sys.path.insert(0, '.'); "
                f"os.environ['CATALOG_DB_URL'] = 'file://{CATALOG / version}/data/catalog.json'; "
                "sys.dont_write_bytecode = True; import app; print(app.search('', 'wadden')['count'])")
        out = subprocess.run(["python3", "-c", code], cwd=CATALOG / version, capture_output=True, text=True, check=True).stdout
        assert out.strip() == count, version


def test_the_baseline_differs_from_1_9_1_by_its_memory_and_the_bug_only():
    assert changed(CATALOG / "1.9.1", CATALOG / "1.12.0") == ["VERSION", "app.py", "chart/Chart.yaml", "chart/values.yaml", "tests/test_app.py"]
    values = changed_lines(CATALOG / "1.9.1/chart/values.yaml", CATALOG / "1.12.0/chart/values.yaml")
    assert sorted(line for line in values if line.startswith("+ ")) == ["+   limits: { cpu: 250m, memory: 96Mi }", "+   requests: { cpu: 10m, memory: 48Mi }"]


def test_giving_the_memory_back_is_the_charts_reservation_and_nothing_else():
    assert changed(FORECAST / "2.4.0", FORECAST / "2.4.1") == ["VERSION", "chart/Chart.yaml", "chart/values.yaml"]
    lines = changed_lines(FORECAST / "2.4.0/chart/values.yaml", FORECAST / "2.4.1/chart/values.yaml")
    assert [line for line in lines if not line.startswith("- #")] == [
        "-   requests: { cpu: 10m, memory: 256Mi }", "-   limits: { cpu: 250m, memory: 256Mi }",
        "+   requests: { cpu: 10m, memory: 48Mi }", "+   limits: { cpu: 250m, memory: 96Mi }"]


def test_main_starts_with_forecast_2_4_0():
    assert changed(ROOT / "services/forecast", FORECAST / "2.4.0") == []


@pytest.mark.parametrize("name, version", [("catalog", "1.12.0"), ("catalog", "1.12.1"), ("forecast", "2.4.0"), ("forecast", "2.4.1")])
def test_each_state_says_its_own_version(name, version):
    folder = ROOT / "demo-states" / name / version
    chart = yaml.safe_load((folder / "chart/Chart.yaml").read_text())
    assert (folder / "VERSION").read_text().strip() == version == chart["version"] == chart["appVersion"]


@pytest.mark.parametrize("name, version", [("catalog", "1.12.0"), ("catalog", "1.12.1"), ("forecast", "2.4.0"), ("forecast", "2.4.1")])
def test_no_state_adds_a_stage(name, version, monkeypatch):
    config = {**service(name), "dir": ROOT / "demo-states" / name / version}
    stages = run_state.stages_for(config)
    assert "Blip check" not in stages and "Migration check" not in stages and "Rollback check" not in stages
    assert not (config["dir"] / "database").exists() and not (config["dir"] / "migrations").exists()


def test_every_state_uses_the_base_image_catalog_already_runs():
    base = json.loads((CATALOG / "1.9.1/image.json").read_text())["base"]
    for folder in (CATALOG / "1.12.0", CATALOG / "1.12.1", FORECAST / "2.4.0", FORECAST / "2.4.1"):
        assert json.loads((folder / "image.json").read_text())["base"] == base


def render(folder, name, namespace):
    result = subprocess.run([helm, "template", name, str(folder / "chart"), "--namespace", namespace, "--set", f"image.digest={DIGEST}"],
                            capture_output=True, text=True, check=True)
    return {document["kind"]: document for document in yaml.safe_load_all(result.stdout) if document}


@pytest.mark.skipif(not helm, reason="helm isn't installed")
def test_catalogs_rollout_needs_a_surge_pod_and_forecasts_doesnt():
    catalog = render(CATALOG / "1.12.1", "catalog", "staging")["Deployment"]["spec"]
    assert catalog["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1}
    assert catalog["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] == "48Mi"
    forecast = render(FORECAST / "2.4.1", "forecast", "forecast")["Deployment"]["spec"]
    assert forecast["strategy"]["rollingUpdate"] == {"maxUnavailable": 1, "maxSurge": 0}
    assert forecast["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] == "48Mi"


@pytest.mark.skipif(not helm, reason="helm isn't installed")
def test_forecasts_pods_carry_the_labels_kube_selects_on():
    labels = render(FORECAST / "2.4.0", "forecast", "forecast")["Deployment"]["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/instance"] == "forecast"
    assert labels["app.kubernetes.io/version"] == "2.4.0"


def test_the_services_entry_is_what_the_pipeline_and_the_slack_handler_read():
    config = service("forecast")
    assert config["slack"]["options"] == []                     # so never a standard change: Alex decides
    assert "standard" not in config["servicenow"].get("change", "")
    assert "staging" not in config["environments"]              # the shared pipeline skips Staging and Check staging
    production = config["environments"]["production"]
    assert production["namespace"] == "forecast" and production["cmdb_item"] == config["servicenow"]["cmdb_item"]
    assert production["node_port"] not in {env["node_port"] for env in service("catalog")["environments"].values()}
    assert config["kubernetes"]["release"] == "forecast"
    assert config["jcr"] == {"shared": True}


def test_the_comment_in_2_4_0_gives_a_reason_and_no_label_says_planted():
    text = (FORECAST / "2.4.0/chart/values.yaml").read_text()
    assert "month-end run" in text
    for word in ("padded", "planted", "staged", "simulated"):
        assert word not in text


def test_nothing_on_screen_in_these_states_is_a_word_the_rules_keep_off_it():
    for folder in (FORECAST / "2.4.0", FORECAST / "2.4.1"):
        for name, path in files(folder).items():
            if path.suffix in (".py", ".yaml", ".json", ".tpl"):
                assert "DORA" not in path.read_text(), name
    assert Path(FORECAST / "2.4.0/app.py").read_text().count("Demo system") == 0
