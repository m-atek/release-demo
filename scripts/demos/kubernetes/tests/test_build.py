import json
import zipfile
from pathlib import Path

import pytest

import build
import oci

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def no_registry(monkeypatch, base_image):
    monkeypatch.setattr(oci, "pull", lambda reference, platform, cache: base_image)


def test_same_source_same_package(tmp_path):
    first = build.build("catalog", version_dir=ROOT / "demo-states/catalog/1.9.0", out=tmp_path / "a", commit="abc")
    second = build.build("catalog", version_dir=ROOT / "demo-states/catalog/1.9.0", out=tmp_path / "b", commit="abc")
    assert first["sha256"] == second["sha256"]
    assert first["image_digest"] == second["image_digest"] and first["chart_digest"] == second["chart_digest"]


def test_another_commit_another_image(tmp_path):
    one = build.build("catalog", version_dir=ROOT / "demo-states/catalog/1.9.0", out=tmp_path / "a", commit="abc")
    two = build.build("catalog", version_dir=ROOT / "demo-states/catalog/1.9.0", out=tmp_path / "b", commit="def")
    assert one["image_digest"] != two["image_digest"]
    assert one["chart_digest"] == two["chart_digest"]  # the chart holds no commit


def test_package_names_its_digests(tmp_path):
    info = build.build("catalog", version_dir=ROOT / "demo-states/catalog/1.9.1", out=tmp_path, commit="abc")
    with zipfile.ZipFile(tmp_path / "catalog" / info["package"]) as archive:
        release = json.loads(archive.read("release.json"))
        names = archive.namelist()
    assert release["version"] == "1.9.1"
    assert release["image"]["digest"] == info["image_digest"]
    assert info["image"] == f"artifactory-demo.es.nl/docker-local/catalog@{info['image_digest']}"
    assert "image/index.json" in names and "chart/index.json" in names
    written = json.loads((tmp_path / "catalog" / "build-info.json").read_text())
    assert written["sha256"] == info["sha256"]


def test_chart_version_must_follow_the_version_file(tmp_path):
    import shutil

    source = tmp_path / "state"
    shutil.copytree(ROOT / "demo-states/catalog/1.8.2", source)
    (source / "VERSION").write_text("1.8.3\n")
    with pytest.raises(SystemExit):
        build.build("catalog", version_dir=source, out=tmp_path / "out", commit="abc")
