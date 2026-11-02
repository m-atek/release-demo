import io
import json
import os
import tarfile
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


# What these versions built before /app/vendor existed, with this fake base image and commit abc.
BEFORE = {"1.8.2": "sha256:652f35b1dbdf27e104f9ffe928a4b55afaa698904f8866316354138984dc5a37",
          "1.9.0": "sha256:ce0b5666d9e5b8423e8d403b3c6570d504eb7c0fe3baf87a3dd07d69caaaca13",
          "1.9.1": "sha256:fa14dec19afa8095f2143f94418f11c2ff17226a351689558ae7702dbfd404af"}


@pytest.mark.parametrize("version", BEFORE)
def test_a_version_without_requirements_builds_byte_for_byte_as_before(version, tmp_path, monkeypatch):
    monkeypatch.setattr(build, "install_requirements", lambda *args: pytest.fail("nothing to install"))
    info = build.build("catalog", version_dir=ROOT / f"demo-states/catalog/{version}", out=tmp_path, commit="abc")
    assert info["image_digest"] == BEFORE[version]


def fake_install(stamp=None):
    """What pip --target leaves: packages and their metadata, a cache to leave out, any file time."""
    def install(requirements, target, runtime):
        assert runtime == "python3.13" and requirements.name == "requirements.txt"
        for name, text in (("pg8000/__init__.py", "x = 1"), ("pg8000/native.py", "y = 2"),
                           ("pg8000-1.31.5.dist-info/METADATA", "Name: pg8000"), ("six.py", "z = 3"),
                           ("pg8000/__pycache__/native.cpython-313.pyc", "cache")):
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            if stamp:
                os.utime(path, (stamp, stamp))
    return install


def image_files(tmp_path, info):
    """The files of the image's own layer, with their modes."""
    with zipfile.ZipFile(tmp_path / "catalog" / info["package"]) as archive:
        index = json.loads(archive.read("image/index.json"))
        manifest = json.loads(archive.read(f"image/blobs/{index['manifests'][0]['digest'].replace(':', '/')}"))
        layer = archive.read(f"image/blobs/{manifest['layers'][-1]['digest'].replace(':', '/')}")
    with tarfile.open(fileobj=io.BytesIO(layer)) as tar:
        return {member.name: member for member in tar.getmembers()}


@pytest.mark.parametrize("version", ["1.10.0", "1.10.1"])
def test_the_postgres_versions_carry_their_driver_under_app_vendor(version, tmp_path, monkeypatch):
    monkeypatch.setattr(build, "install_requirements", fake_install())
    info = build.build("catalog", version_dir=ROOT / f"demo-states/catalog/{version}", out=tmp_path, commit="abc")
    files = image_files(tmp_path, info)
    assert {"app/app.py", "app/VERSION", "app/build_info.json", "app/vendor/pg8000/native.py", "app/vendor/six.py",
            "app/vendor/pg8000-1.31.5.dist-info/METADATA"} <= set(files)
    assert files["app/vendor/pg8000"].isdir() and files["app/vendor"].isdir()
    assert not [name for name in files if "__pycache__" in name or name.endswith(".pyc")]
    # The catalog lives in Postgres now: no file to fall back on, and no seed or test in the image.
    assert not [name for name in files if name.startswith(("app/data", "app/database", "app/tests", "app/chart"))]
    assert len([name for name in files if name == "app"]) == 1


def test_the_vendored_image_is_reproducible(tmp_path, monkeypatch):
    """Check 15's offline half: the installed files' times don't reach the digests."""
    source = ROOT / "demo-states/catalog/1.10.0"
    monkeypatch.setattr(build, "install_requirements", fake_install(1_000_000))
    first = build.build("catalog", version_dir=source, out=tmp_path / "a", commit="abc")
    monkeypatch.setattr(build, "install_requirements", fake_install(2_000_000))
    second = build.build("catalog", version_dir=source, out=tmp_path / "b", commit="abc")
    assert first["sha256"] == second["sha256"] and first["image_digest"] == second["image_digest"]
