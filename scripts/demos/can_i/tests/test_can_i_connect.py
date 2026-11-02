"""Sam's kubeconfig: the key stays on the Mac, and the key file goes."""

import base64
import json
import subprocess

import pytest

import connect
import remote


def fake_signing(monkeypatch, asked):
    def run(command, timeout):
        asked.append(command)
        return 0, 'k3s says\n{"certificate": "Q0VSVA==", "ca": "Q0E="}\n'

    monkeypatch.setattr(remote, "run", run)


def test_the_request_names_sam_in_his_group(tmp_path):
    key, pem = connect.request(tmp_path)
    subject = subprocess.run(["openssl", "req", "-noout", "-subject", "-nameopt", "RFC2253"], input=pem,
                             capture_output=True, text=False).stdout.decode()
    assert "CN=sam" in subject and "O=release-demo:developers" in subject
    assert oct(key.stat().st_mode)[-3:] == "600" and not (tmp_path / "sam.csr").exists()


def test_the_kubeconfig_points_at_the_tunnel_and_production(monkeypatch, tmp_path):
    fake_signing(monkeypatch, [])
    path = connect.certificate(tmp_path)
    config = json.loads(path.read_text())
    assert path == tmp_path / "sam" and oct(path.stat().st_mode)[-3:] == "600"
    assert config["clusters"][0]["cluster"]["server"] == "https://127.0.0.1:16443"
    assert config["contexts"][0]["context"]["namespace"] == "production"
    assert config["users"][0]["name"] == "sam"
    assert config["users"][0]["user"]["client-certificate-data"] == "Q0VSVA=="
    assert not (tmp_path / "sam.key").exists() and not (tmp_path / "sam.csr").exists()


def test_a_wider_kubeconfig_is_closed_again(monkeypatch, tmp_path):
    fake_signing(monkeypatch, [])
    (tmp_path / "sam").write_text("old")
    (tmp_path / "sam").chmod(0o644)
    assert oct(connect.certificate(tmp_path).stat().st_mode)[-3:] == "600"


def test_the_key_never_leaves_the_mac(monkeypatch, tmp_path):
    asked = []
    fake_signing(monkeypatch, asked)
    connect.certificate(tmp_path)
    assert len(asked) == 1 and asked[0].startswith(f"{remote.K3S} developer ")
    sent = base64.b64decode(asked[0].split()[-1]).decode()
    assert "CERTIFICATE REQUEST" in sent and "PRIVATE KEY" not in sent


def test_a_refused_request_removes_the_key_too(monkeypatch, tmp_path):
    monkeypatch.setattr(remote, "run", lambda command, timeout: (1, "Not a developer's request"))
    with pytest.raises(SystemExit):
        connect.certificate(tmp_path)
    assert not (tmp_path / "sam.key").exists() and not (tmp_path / "sam").exists()
