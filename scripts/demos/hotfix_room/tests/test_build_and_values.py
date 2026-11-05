"""forecast through video 8's own build and values, with a tiny base image and no registry: what the pipeline's Build and Staging would hand Helm."""

import json
import shutil
import subprocess
import zipfile

import pytest
import yaml

import build
import deploy
import notify
import oci
import run_state
from common import ROOT, service

helm = shutil.which("helm")


@pytest.fixture
def base_image(tmp_path, monkeypatch):
    """A tiny base image, as oci.pull returns one, without a registry."""
    layer = oci._gzip(oci._tar([("bin", None, 0o755), ("bin/sh", b"#!", 0o755)]))
    path = tmp_path / "layer"
    path.write_bytes(layer)
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": {"Env": ["PATH=/bin"]},
                         "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64]}, "history": []}).encode()
    descriptor = {"mediaType": oci.LAYER, "digest": oci.digest(layer), "size": len(layer)}
    image = oci.Image("docker.io/library/base:1@sha256:" + "1" * 64, "sha256:" + "2" * 64, {"layers": [descriptor]}, config, [(descriptor, path)])
    monkeypatch.setattr(oci, "pull", lambda reference, platform, cache: image)
    return image


def built(name, version, out, commit="abc123"):
    return build.build(name, version_dir=ROOT / "demo-states" / name / version, out=out, commit=commit)


def test_forecast_builds_with_video_8s_build_and_says_its_own_name(base_image, tmp_path):
    info = built("forecast", "2.4.1", tmp_path / "a")
    assert info["service"] == "forecast" and info["version"] == "2.4.1"
    assert info["image"].startswith("artifactory-demo.es.nl/docker-local/forecast@sha256:")
    with zipfile.ZipFile(tmp_path / "a" / "forecast" / info["package"]) as archive:
        release = json.loads(archive.read("release.json"))
    assert release["chart"]["repository"] == "helm-local/forecast"


def test_the_same_commit_gives_the_same_package_twice_so_the_second_try_is_the_same_hotfix(base_image, tmp_path):
    for name, version in (("catalog", "1.12.1"), ("forecast", "2.4.1")):
        one, two = built(name, version, tmp_path / "one"), built(name, version, tmp_path / "two")
        assert (one["sha256"], one["image_digest"], one["chart_digest"]) == (two["sha256"], two["image_digest"], two["chart_digest"])


def test_another_commit_is_another_package_which_is_why_the_reset_stages_both_at_once(base_image, tmp_path):
    assert built("catalog", "1.12.1", tmp_path / "one")["sha256"] != built("catalog", "1.12.1", tmp_path / "two", commit="def456")["sha256"]


def test_the_two_versions_differ_in_what_the_chart_reserves(base_image, tmp_path):
    assert built("forecast", "2.4.0", tmp_path / "a")["chart_digest"] != built("forecast", "2.4.1", tmp_path / "b")["chart_digest"]


@pytest.mark.skipif(not helm, reason="helm isn't installed")
@pytest.mark.parametrize("version, memory", [("2.4.0", "256Mi"), ("2.4.1", "48Mi")])
def test_what_helm_gets_from_the_pipeline_renders_forecasts_three_pods(tmp_path, version, memory):
    config = service("forecast")
    release = {"image": {"digest": "sha256:" + "ab" * 32}}
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump(deploy.values(config, "production", release)))
    result = subprocess.run([helm, "template", "forecast", str(ROOT / "demo-states/forecast" / version / "chart"), "--namespace", "forecast",
                             "--values", str(values)], capture_output=True, text=True, check=True)
    found = {document["kind"]: document for document in yaml.safe_load_all(result.stdout) if document}
    spec = found["Deployment"]["spec"]
    assert spec["replicas"] == 3
    assert spec["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] == memory
    assert found["Service"]["spec"]["ports"][0]["nodePort"] == 30083


def test_slack_shows_the_stages_the_row_and_the_lines_in_order():
    stages = run_state.stages_for(service("forecast"))
    state = {"service": "forecast", "version": "2.4.1", "jira_key": "ORD-121", "build": {},
             "stages": {name: {"status": "waiting"} for name in stages},
             "rows": ["Reserved 768 MiB, used 51 MiB. Asks for 144 MiB."], "lines": ["Approved by Alex in ServiceNow."]}
    text = notify.render(state)
    assert text.index("Plan") < text.index("Change") < text.index("Reserved 768 MiB") < text.index("Approved by Alex")


def test_the_line_about_the_scheduler_reads_as_the_script_quotes_it():
    state = {"service": "catalog", "version": "1.12.1", "jira_key": "ORD-120", "build": {}, "stages": {},
             "lines": ["Not ready after 3 minutes. The scheduler: 0/1 nodes are available: 1 Insufficient memory.",
                       "Stopped after 5m 12s. Nothing reached production."]}
    assert notify.render(state).endswith("Not ready after 3 minutes. The scheduler: 0/1 nodes are available: 1 Insufficient memory.\n\n"
                                         "Stopped after 5m 12s. Nothing reached production.")
