import base64
import json
import shutil
import subprocess

import build
import identity
import pytest
from common import ROOT, service
from registry import Layout


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def signed(tmp_path):
    """A token signed like Jenkins signs it, and the key set the issuer would publish."""
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    key = tmp_path / "key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    modulus = subprocess.run(["openssl", "rsa", "-in", str(key), "-noout", "-modulus"], check=True,
                             capture_output=True, text=True).stdout.split("=")[1].strip()
    header = b64(json.dumps({"kid": "oidc-aws", "alg": "RS256", "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iss": "https://issuer.example", "sub": "https://jenkins-demo.es.nl/job/release/",
                             "aud": ["sts.amazonaws.com"], "exp": 1793123938, "build_number": 17}).encode())
    signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=f"{header}.{claims}".encode(),
                               check=True, capture_output=True).stdout
    token = f"{header}.{claims}.{b64(signature)}"
    jwk = {"kid": "oidc-aws", "kty": "RSA", "e": b64((65537).to_bytes(3, "big")), "n": b64(bytes.fromhex(modulus))}
    return token, {"keys": [jwk]}


def test_a_token_matching_the_published_key_passes(monkeypatch, signed):
    token, keys = signed
    monkeypatch.setattr(identity, "http", lambda method, url, *a, **k: (200, keys))
    identity.verify_signature(token)
    assert "build 17" in identity.describe_token(token)


def test_a_key_set_from_before_the_last_restart_is_named_as_the_cause(monkeypatch, signed):
    token, keys = signed
    keys["keys"][0]["n"] = b64(b"\x01" * 256)
    monkeypatch.setattr(identity, "http", lambda method, url, *a, **k: (200, keys))
    with pytest.raises(identity.SignInError, match="out of date"):
        identity.verify_signature(token)


def test_in_jenkins_a_missing_token_is_an_error_not_a_fallback(monkeypatch):
    monkeypatch.setenv("BUILD_URL", "https://jenkins-demo.es.nl/job/release/17/")
    with pytest.raises(SystemExit):
        identity.sign_in("aws", {})


def test_token_lines_show_the_expiry_never_the_token():
    login = identity.Login("aws", "role release-demo-deploy-quote-service", 1793123938, "token", session=None)
    line = login.line("AWS")
    assert "accepted it" in line and "expires" in line and "UTC" in line


def fake_base(monkeypatch):
    """A tiny base image instead of the real one from ECR Public."""
    layer = build.tar_layer([("bin", None, 0o755)])
    base_layer = {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": build.sha256(layer[0]),
                  "size": len(layer[0])}
    manifest = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "layers": [base_layer],
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json"}}
    config = {"architecture": "amd64", "os": "linux", "config": {"Env": ["PATH=/bin"]},
              "rootfs": {"type": "layers", "diff_ids": [layer[1]]}, "history": []}

    class Blobs:
        def blob(self, repository, digest):
            return layer[0]

    monkeypatch.setattr(build, "pull", lambda reference: (manifest, config, Blobs(), "base"))
    monkeypatch.setattr(build, "adapter_binary", lambda reference: b"adapter")


def test_the_same_source_and_commit_give_the_same_image(monkeypatch, tmp_path):
    fake_base(monkeypatch)
    config = service("quote-service")
    source = ROOT / "demo-states" / "quote-service" / "1.2.0"
    first, _ = build.build_image(config, source, "1.2.0", "abc1234", "2026-10-27T10:00:00Z", tmp_path / "a")
    again, _ = build.build_image(config, source, "1.2.0", "abc1234", "2026-10-27T10:00:00Z", tmp_path / "b")
    other, _ = build.build_image(config, source, "1.2.0", "def5678", "2026-10-27T10:00:00Z", tmp_path / "c")
    assert first == again != other


def test_the_image_runs_the_service_with_the_adapter_and_no_tests(monkeypatch, tmp_path):
    fake_base(monkeypatch)
    config = service("quote-service")
    digest, _ = build.build_image(config, ROOT / "demo-states" / "quote-service" / "1.2.0", "1.2.0", "abc1234",
                                  "2026-10-27T10:00:00Z", tmp_path / "image")
    layout = Layout(tmp_path / "image")
    manifest = json.loads(layout.manifest("", digest)[1])
    image_config = json.loads(layout.blob("", manifest["config"]["digest"]))
    assert image_config["config"]["Cmd"] == ["python3", "/app/server.py"]
    assert "AWS_LWA_READINESS_CHECK_PATH=/health" in image_config["config"]["Env"]
    assert image_config["config"]["Labels"]["org.opencontainers.image.version"] == "1.2.0"
    assert len(image_config["rootfs"]["diff_ids"]) == len(manifest["layers"]) == 3
    import gzip, io, tarfile  # noqa: E401
    app = tarfile.open(fileobj=io.BytesIO(gzip.decompress(layout.blob("", manifest["layers"][-1]["digest"]))))
    names = app.getnames()
    assert "app/server.py" in names and "app/build_info.json" in names
    assert not [n for n in names if "tests" in n]
