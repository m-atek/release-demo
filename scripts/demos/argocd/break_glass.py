"""Break-glass access to production on the cluster host, for the on-call engineer.

    .venv/bin/python scripts/demos/argocd/break_glass.py pieter      then, in another terminal:
    export KUBECONFIG=~/.kube/release-demo/pieter

Makes a key on the Mac and a certificate request for the person's cluster
user (people.yaml kubernetes_user, CN pieter-oncall) in the group
release-demo:break-glass. The cluster host approves it through SSM Run
Command and sends back only the public certificate, valid for an hour; the
key never leaves the Mac, and no key, certificate or token lands in SSM's
command history. Then it holds an SSM port forward to the Kubernetes API on
127.0.0.1:26443 until Ctrl-C. The role break-glass lets the group read the
catalog's deployments, replica sets and pods in production, and patch a
deployment or its scale; nothing in any other namespace.

Needs the Session Manager plugin for the AWS CLI:
brew install --cask session-manager-plugin
"""

import argparse
import base64
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[2]))
import host  # noqa: E402
from common import ROOT, aws_session, fail  # noqa: E402

FOLDER = Path.home() / ".kube" / "release-demo"
LOCAL_PORT = 26443
GROUP = "release-demo:break-glass"


def person(key):
    everyone = yaml.safe_load((ROOT / "people.yaml").read_text())
    if key not in everyone or not everyone[key].get("kubernetes_user"):
        fail(f"people.yaml has no {key} with a kubernetes_user. Break-glass users: "
             f"{', '.join(k for k, p in everyone.items() if p.get('kubernetes_user')) or 'none'}")
    return everyone[key]


def request(user, folder=FOLDER):
    """A new key and a certificate request for user in the break-glass group. Returns (key path, request PEM)."""
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    key, csr = folder / f"{user}.key", folder / f"{user}.csr"
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr),
                    "-subj", f"/O={GROUP}/CN={user}"], check=True, capture_output=True)
    key.chmod(0o600)
    pem = csr.read_bytes()
    csr.unlink()
    return key, pem


def kubeconfig(user, signed, key):
    return {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "cluster-host", "cluster": {
            "server": f"https://127.0.0.1:{LOCAL_PORT}", "certificate-authority-data": signed["ca"]}}],
        "users": [{"name": user, "user": {"client-certificate-data": signed["certificate"],
                                          "client-key-data": base64.b64encode(key.read_bytes()).decode()}}],
        "contexts": [{"name": "break-glass", "context": {"cluster": "cluster-host", "user": user,
                                                         "namespace": "production"}}],
        "current-context": "break-glass",
    }


def expiry(certificate_b64):
    text = base64.b64decode(certificate_b64).decode()
    end = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=text, capture_output=True,
                         text=True).stdout.strip().removeprefix("notAfter=")
    moment = datetime.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("person", help="a person in people.yaml with a kubernetes_user, such as pieter")
    args = parser.parse_args()
    if not shutil.which("session-manager-plugin"):
        fail("The AWS CLI needs its Session Manager plugin for the tunnel: brew install --cask session-manager-plugin")
    user = person(args.person)["kubernetes_user"]
    key, pem = request(user)
    name = f"break-glass-{user}-{int(time.time())}"
    code, output = host.run(f"python3 scripts/demos/argocd/host/cluster.py break-glass "
                            f"{base64.b64encode(pem).decode()} {name}", timeout=120)
    if code != 0:
        key.unlink(missing_ok=True)
        fail(f"The cluster didn't sign the request:\n{output}\nIs it up? scripts/demo.sh up argocd")
    signed = json.loads(output.strip().splitlines()[-1])
    path = FOLDER / args.person
    path.touch(mode=0o600)
    path.write_text(json.dumps(kubeconfig(user, signed, key), indent=2) + "\n")
    key.unlink()
    instance = host.instance(aws_session())[0]
    env = {**os.environ, "AWS_PROFILE": os.environ.get("AWS_PROFILE", "release-demo"), "AWS_REGION": "eu-west-1"}
    tunnel = subprocess.Popen(
        ["aws", "ssm", "start-session", "--target", instance, "--document-name", "AWS-StartPortForwardingSession",
         "--parameters", json.dumps({"portNumber": ["6443"], "localPortNumber": [str(LOCAL_PORT)]})],
        env=env, stdout=subprocess.DEVNULL)
    print(f"""{user}, group {GROUP}: a certificate until {expiry(signed['certificate']):%H:%M:%S}, in {path}.
The tunnel to the cluster's API stays open until Ctrl-C. In the video's terminal panes:

  export KUBECONFIG={path}
  kubectl -n production get deploy catalog -w -o custom-columns=WANTED:.spec.replicas,READY:.status.readyReplicas
  kubectl -n production scale deploy/catalog --replicas=5

Checks: kubectl auth can-i patch deployments/scale -n production   (yes)
        kubectl auth can-i patch deployments -n staging           (no)
        kubectl auth can-i get secrets -n production              (no)""", flush=True)
    signal.signal(signal.SIGINT, lambda *_: tunnel.terminate())
    tunnel.wait()


if __name__ == "__main__":
    main()
