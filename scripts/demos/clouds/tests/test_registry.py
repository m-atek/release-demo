import json

import pytest
from fake_jcr import Handler, Store, start
from registry import Layout, Registry, RegistryError, copy, image_manifest, sha256


@pytest.fixture
def jcr():
    Handler.store = Store()
    server, url = start()
    yield Registry(url.split("//")[1], "release-pipeline", "token", name="JFrog")
    server.shutdown()


def image(tmp_path):
    """A one-layer image in an OCI layout: its digest and layout."""
    layout = Layout(tmp_path / "image")
    layer = layout.write_blob(b"layer bytes")
    config = json.dumps({"rootfs": {"diff_ids": [sha256(b"layer bytes")]}}).encode()
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                           "digest": layout.write_blob(config), "size": len(config)},
                "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": layer, "size": 11}]}
    body = json.dumps(manifest).encode()
    return layout.put_manifest("", "1.2.0", manifest["mediaType"], body), layout


def test_copy_keeps_the_digest_and_signs_in_with_a_bearer_token(tmp_path, jcr):
    digest, layout = image(tmp_path)
    assert copy(layout, "", digest, jcr, "docker-local/quote-service", "1.2.0-17", log=lambda _: None) == digest
    assert jcr.digest("docker-local/quote-service", "1.2.0-17") == digest
    assert jcr.tokens  # the challenge gave a bearer token
    other = Layout(tmp_path / "copy")
    copy(jcr, "docker-local/quote-service", digest, other, "", "1.2.0-17", log=lambda _: None)
    assert other.image()["digest"] == digest


def test_copy_twice_sends_nothing_the_second_time(tmp_path, jcr):
    digest, layout = image(tmp_path)
    lines = []
    copy(layout, "", digest, jcr, "docker-local/quote-service", "1.2.0-17", log=lines.append)
    copy(layout, "", digest, jcr, "docker-local/quote-service", "1.2.0-17", log=lines.append)
    assert "already holds" in lines[-1]


def test_a_tag_holding_another_image_stops_the_copy(tmp_path, jcr):
    digest, layout = image(tmp_path)
    copy(layout, "", digest, jcr, "docker-local/quote-service", "1.2.0-17", log=lambda _: None)
    other_digest, other = image(tmp_path / "other")
    body = json.dumps({**json.loads(other.manifest("", other_digest)[1]), "annotations": {"x": "y"}}).encode()
    new_digest = other.put_manifest("", "1.2.0", "application/vnd.oci.image.manifest.v1+json", body)
    with pytest.raises(RegistryError, match="already holds"):
        copy(other, "", new_digest, jcr, "docker-local/quote-service", "1.2.0-17", log=lambda _: None)


def test_a_missing_tag_has_no_digest(jcr):
    assert jcr.digest("docker-local/quote-service", "nothing") is None


def test_an_index_resolves_to_its_linux_amd64_image(tmp_path):
    digest, layout = image(tmp_path)
    media_type, body, _ = layout.manifest("", digest)
    index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"mediaType": media_type, "digest": digest, "size": len(body), "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()

    class Source:
        def manifest(self, repository, reference):
            if reference == "index":
                return "application/vnd.oci.image.index.v1+json", index, sha256(index)
            return layout.manifest(repository, reference)

    assert image_manifest(Source(), "", "index")[2] == digest
