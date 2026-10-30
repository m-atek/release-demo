"""deploy/production/catalog: 1.9.1's chart, vendored, with production's values."""

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
FOLDER = ROOT / "deploy" / "production" / "catalog"
PACKAGE = ROOT / "demo-states" / "catalog" / "1.9.1" / "chart"


def values():
    return yaml.safe_load((FOLDER / "values.yaml").read_text())


def test_the_templates_are_the_packages_unchanged():
    vendored = sorted(p.name for p in (FOLDER / "templates").iterdir())
    assert vendored == sorted(p.name for p in (PACKAGE / "templates").iterdir())
    for name in vendored:
        assert (FOLDER / "templates" / name).read_text() == (PACKAGE / "templates" / name).read_text(), name


def test_the_chart_is_1_9_1():
    chart = yaml.safe_load((FOLDER / "Chart.yaml").read_text())
    package = yaml.safe_load((PACKAGE / "Chart.yaml").read_text())
    assert (chart["name"], str(chart["version"]), str(chart["appVersion"])) == ("catalog", "1.9.1", "1.9.1")
    assert {k: v for k, v in chart.items() if k != "description"} == {k: v for k, v in package.items()
                                                                       if k != "description"}
    assert chart["description"] == "The catalog service of the release demo, in production on the cluster host."


def test_production_runs_three_pods_pinned_by_digest_or_not_at_all():
    found = values()
    assert found["replicas"] == 3 and found["environment"] == "production"
    assert found["image"]["repository"] == "artifactory-demo.es.nl/docker-local/catalog"
    # Empty until reset.py pins 1.9.1's digest; the chart then refuses to render, so nothing deploys.
    assert found["image"]["digest"] == "" or re.fullmatch(r"sha256:[0-9a-f]{64}", found["image"]["digest"])
    assert "required" in (FOLDER / "templates" / "deployment.yaml").read_text()


def test_production_has_the_settings_services_yaml_gives_it():
    from common import service

    config = service("catalog")
    assert config["environments"]["production"]["settings"] == {}
    package = yaml.safe_load((PACKAGE / "values.yaml").read_text())
    assert values()["settings"] == package["settings"]
    assert config["gitops"]["path"] == "deploy/production/catalog"


@pytest.mark.skipif(not os.environ.get("JCR_TOKEN"), reason="needs JFrog Container Registry (JCR_URL, JCR_TOKEN)")
def test_the_digest_is_1_9_1s_in_jfrog():
    """On the tools host: JCR_URL=http://localhost:8082 JCR_TOKEN=... pytest scripts/demos/argocd/tests."""
    base = os.environ.get("JCR_URL", "http://localhost:8082").rstrip("/")
    request = urllib.request.Request(f"{base}/artifactory/demos-releases/catalog/1.9.1/build-info.json",
                                     headers={"Authorization": f"Bearer {os.environ['JCR_TOKEN']}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert values()["image"]["digest"] == json.loads(response.read())["image_digest"]
