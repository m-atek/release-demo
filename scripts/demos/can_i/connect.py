"""Sam's kubeconfig and the admin's for video 26's terminals, on the Mac.

    .venv/bin/python scripts/demos/can_i/connect.py    then, in the two terminals:
    export KUBECONFIG=~/.kube/release-demo/sam
    export KUBECONFIG=~/.kube/release-demo/config

Makes a key on the Mac and a certificate request for CN sam in the group
release-demo:developers. The cluster signs it for 8 hours through SSM Run
Command and sends back only public certificates; the key never leaves the Mac.
It writes ~/.kube/release-demo/sam (server 127.0.0.1:16443, namespace
production, mode 600) and removes the key file. Then it runs video 8's
connect.py, which makes the admin's kubeconfig and holds the tunnels until
Ctrl-C. Sam isn't in people.yaml as kubernetes_user: break_glass.py reads that
key as "may hold break-glass". It prints the home folder: keep it off camera.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import remote  # noqa: E402
from common import fail  # noqa: E402

FOLDER = Path.home() / ".kube" / "release-demo"
USER = "sam"
GROUP = "release-demo:developers"
SERVER = "https://127.0.0.1:16443"  # video 8's tunnel to the cluster's API


def request(folder=FOLDER):
    """A new key and a certificate request. Returns (key path, request PEM). The request stays no longer than this."""
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    key, csr = folder / f"{USER}.key", folder / f"{USER}.csr"
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr),
                    "-subj", f"/O={GROUP}/CN={USER}"], check=True, capture_output=True)
    key.chmod(0o600)
    pem = csr.read_bytes()
    csr.unlink()
    return key, pem


def kubeconfig(signed, key):
    return {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "release-demo", "cluster": {
            "server": SERVER, "certificate-authority-data": signed["ca"]}}],
        "users": [{"name": USER, "user": {"client-certificate-data": signed["certificate"],
                                          "client-key-data": base64.b64encode(key.read_bytes()).decode()}}],
        "contexts": [{"name": "release-demo", "context": {"cluster": "release-demo", "user": USER,
                                                          "namespace": "production"}}],
        "current-context": "release-demo",
    }


def certificate(folder=FOLDER):
    """Sam's kubeconfig, signed by the cluster. Returns its path. The key file is gone either way."""
    key, pem = request(folder)
    try:
        code, output = remote.run(f"{remote.K3S} developer {base64.b64encode(pem).decode()}", timeout=120)
        if code != 0:
            fail(f"The cluster didn't sign the request:\n{output}\nIs it up? scripts/demo.sh up can-i")
        signed = json.loads(output.strip().splitlines()[-1])
        path = folder / USER
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(json.dumps(kubeconfig(signed, key), indent=2) + "\n")
    finally:
        key.unlink(missing_ok=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    if not shutil.which("session-manager-plugin"):
        fail("The AWS CLI needs its Session Manager plugin for the tunnels: brew install --cask session-manager-plugin")
    print(f"{USER}, group {GROUP}: a certificate for 8 hours in {certificate()}.", flush=True)
    video8 = HERE.parent / "kubernetes" / "connect.py"
    os.execv(sys.executable, [sys.executable, str(video8)])


if __name__ == "__main__":
    main()
