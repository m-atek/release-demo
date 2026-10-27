import io
import json
import tarfile
from pathlib import Path

import oci

CHART = Path(__file__).resolve().parents[4] / "services" / "catalog" / "chart"


def test_parse_reference():
    assert oci.parse_reference("docker.io/library/python:3.13@sha256:ab") == (
        "https://registry-1.docker.io", "library/python", "3.13", "sha256:ab")
    assert oci.parse_reference("python:3.13") == ("https://registry-1.docker.io", "library/python", "3.13", None)
    assert oci.parse_reference("registry.k8s.io/ksm/ksm:v2@sha256:cd")[:2] == ("https://registry.k8s.io", "ksm/ksm")


def test_layer_is_the_same_for_the_same_files(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    first = oci.app_layer(oci.tree(tmp_path, ["app.py"], "app"))
    (tmp_path / "app.py").touch()  # a new mtime changes nothing
    assert oci.app_layer(oci.tree(tmp_path, ["app.py"], "app")) == first


def test_layer_holds_the_files_under_the_prefix_owned_by_root(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "catalog.json").write_text("{}")
    layer, _ = oci.app_layer(oci.tree(tmp_path, ["data"], "app", extra={"build_info.json": b"{}"}))
    with tarfile.open(fileobj=io.BytesIO(layer)) as archive:
        members = {m.name: m for m in archive.getmembers()}
    assert set(members) == {"app", "app/data", "app/data/catalog.json", "app/build_info.json"}
    assert all(m.uid == 0 and m.mtime == 0 for m in members.values())


def test_assemble_adds_one_layer_and_our_command(base_image):
    layer, diff_id = oci.app_layer([("app", None, 0o755)])
    settings = {"cmd": ["python3", "/app/app.py"], "workdir": "/app", "user": "10001:10001",
                "env": ["PORT=8080"], "ports": ["8080/tcp"]}
    manifest, blobs = oci.assemble(base_image, layer, diff_id, settings, {"org.opencontainers.image.version": "1.0.0"})
    parsed = json.loads(manifest)
    config = json.loads(blobs[parsed["config"]["digest"]])
    assert len(parsed["layers"]) == 2
    assert config["config"]["Cmd"] == ["python3", "/app/app.py"]
    assert config["config"]["User"] == "10001:10001"
    assert config["config"]["Env"] == ["PATH=/bin", "PORT=8080"]
    assert config["rootfs"]["diff_ids"][-1] == diff_id
    assert parsed["annotations"]["org.opencontainers.image.base.digest"] == base_image.manifest_digest
    assert oci.assemble(base_image, layer, diff_id, settings, {"org.opencontainers.image.version": "1.0.0"})[0] == manifest


def test_chart_package_is_reproducible_and_named_like_helm_s():
    first, metadata = oci.package_chart(CHART)
    assert oci.package_chart(CHART)[0] == first
    with tarfile.open(fileobj=io.BytesIO(first)) as archive:
        names = archive.getnames()
    assert "catalog/Chart.yaml" in names and "catalog/templates/deployment.yaml" in names
    manifest, blobs = oci.chart_artifact(first, metadata)
    parsed = json.loads(manifest)
    assert parsed["config"]["mediaType"] == oci.HELM_CONFIG
    assert parsed["layers"][0]["mediaType"] == oci.HELM_LAYER
    assert json.loads(blobs[parsed["config"]["digest"]])["name"] == "catalog"


def test_layout_round_trip_and_push(tmp_path, base_image, registry):
    client, server = registry
    layer, diff_id = oci.app_layer([("app", None, 0o755)])
    manifest, blobs = oci.assemble(base_image, layer, diff_id, {"cmd": ["x"], "workdir": "/app", "user": "1"}, {})
    written = oci.write_layout(tmp_path / "image", manifest, blobs, "1.0.0")
    read_digest, read_manifest, read_blobs = oci.read_layout(tmp_path / "image")
    assert read_digest == written and read_manifest == manifest and set(read_blobs) == set(blobs)

    assert oci.push_layout(client, "docker-local/catalog", tmp_path / "image", "1.0.0-abc") == written
    assert set(server.blobs) == set(blobs)
    # Again: every blob is there, so only the manifest goes.
    server.blobs_before = dict(server.blobs)
    assert oci.push_layout(client, "docker-local/catalog", tmp_path / "image", "1.0.0-abc") == written


def test_a_changed_blob_is_refused(tmp_path, base_image):
    layer, diff_id = oci.app_layer([("app", None, 0o755)])
    manifest, blobs = oci.assemble(base_image, layer, diff_id, {"cmd": ["x"], "workdir": "/app", "user": "1"}, {})
    oci.write_layout(tmp_path / "image", manifest, blobs, "1.0.0")
    victim = tmp_path / "image" / "blobs" / oci.digest(layer).replace(":", "/")
    victim.write_bytes(b"changed")
    try:
        oci.read_layout(tmp_path / "image")
    except RuntimeError as error:
        assert "doesn't match" in str(error)
    else:
        raise AssertionError("a changed blob passed")
