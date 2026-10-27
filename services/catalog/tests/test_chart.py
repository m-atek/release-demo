"""The Helm chart, rendered with helm template. Skipped where helm isn't
installed; the release pipeline's toolchain image has it."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import app

CHART = Path(__file__).resolve().parents[1] / "chart"
DIGEST = "sha256:" + "ab" * 32
helm = shutil.which("helm")
pytestmark = pytest.mark.skipif(not helm, reason="helm isn't installed")


def render(*extra):
    result = subprocess.run([helm, "template", "catalog", str(CHART), "--namespace", "production", *extra],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return {d["kind"]: d for d in yaml.safe_load_all(result.stdout) if d}


def test_chart_and_app_versions_follow_the_version_file():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["version"] == chart["appVersion"] == app.VERSION


def test_image_is_pinned_by_digest():
    deployment = render("--set", f"image.digest={DIGEST}")["Deployment"]
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == f"artifactory-demo.es.nl/docker-local/catalog@{DIGEST}"


def test_no_digest_no_render():
    with pytest.raises(RuntimeError, match="image.digest is required"):
        render()


def test_rolling_update_never_drops_below_the_desired_count():
    spec = render("--set", f"image.digest={DIGEST}")["Deployment"]["spec"]
    assert spec["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}


def test_probes_resources_and_version_label():
    deployment = render("--set", f"image.digest={DIGEST}")["Deployment"]
    pod = deployment["spec"]["template"]
    container = pod["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert set(container["resources"]) == {"requests", "limits"}
    assert pod["metadata"]["labels"]["app.kubernetes.io/version"] == app.VERSION
    # The selector can't change between releases, so it never carries the version.
    assert "app.kubernetes.io/version" not in deployment["spec"]["selector"]["matchLabels"]


def test_settings_become_environment_variables(tmp_path):
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"image": {"digest": DIGEST}, "settings": {"SOME_SETTING": "on"}}))
    container = render("--values", str(values))["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "SOME_SETTING", "value": "on"} in container["env"]


def test_no_pod_disruption_budget():
    assert "PodDisruptionBudget" not in render("--set", f"image.digest={DIGEST}")


def test_every_environment_gets_the_catalog_database():
    container = render("--set", f"image.digest={DIGEST}")["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "CATALOG_DB_URL", "value": "file:///app/data/catalog.json"} in container["env"]
