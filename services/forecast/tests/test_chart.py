"""The Helm chart, rendered with helm template. Skipped where helm isn't
installed; the release pipeline's toolchain image has it."""

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
    result = subprocess.run([helm, "template", "forecast", str(CHART), "--namespace", "forecast", *extra],
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
    assert image == f"artifactory-demo.es.nl/docker-local/forecast@{DIGEST}"


def test_no_digest_no_render():
    with pytest.raises(RuntimeError, match="image.digest is required"):
        render()


def test_a_rollout_goes_one_pod_at_a_time_with_no_extra_pod():
    deployment = render("--set", f"image.digest={DIGEST}")["Deployment"]
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {"maxUnavailable": 1, "maxSurge": 0}


def test_the_pods_pass_pod_security_baseline():
    spec = render("--set", f"image.digest={DIGEST}")["Deployment"]["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    container = spec["containers"][0]["securityContext"]
    assert container["allowPrivilegeEscalation"] is False
    assert container["capabilities"] == {"drop": ["ALL"]}
    assert "hostPath" not in str(spec)


def test_settings_become_environment_variables(tmp_path):
    deployment = render("--set", f"image.digest={DIGEST}", "--set", "settings.SOME_SETTING=on")["Deployment"]
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "SOME_SETTING", "value": "on"} in env


def test_the_service_listens_on_the_nodes_port():
    service = render("--set", f"image.digest={DIGEST}", "--set", "nodePort=30083")["Service"]
    assert service["spec"]["ports"][0]["nodePort"] == 30083
