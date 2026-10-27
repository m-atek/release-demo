"""kubectl and helm on the Mac, for the video's terminal and for calls.

    .venv/bin/python scripts/demos/kubernetes/connect.py    then, in another terminal:
    export KUBECONFIG=~/.kube/release-demo/config

Makes a key on the Mac and asks the cluster, through SSM Run Command, to sign
a client certificate for it that lasts 8 hours (group release-demo:presenters,
cluster admin). The key never leaves the Mac, and the cluster sends back only
public certificates. Then it holds two SSM tunnels open until Ctrl-C: the
cluster's API on 127.0.0.1:16443, and production's catalog Service on
127.0.0.1:18082, for the prober. The tools host stays closed to inbound
connections. Needs the Session Manager plugin for the AWS CLI:
brew install --cask session-manager-plugin
"""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[2]))
import host  # noqa: E402
from common import aws_session, fail  # noqa: E402

FOLDER = Path.home() / ".kube" / "release-demo"
TUNNELS = {16443: 6443, 18082: 30082}  # on the Mac: on the tools host
SA = "system:serviceaccount:jenkins:release-pipeline"


def certificate():
    FOLDER.mkdir(parents=True, exist_ok=True)
    FOLDER.chmod(0o700)
    key, csr = FOLDER / "presenter.key", FOLDER / "presenter.csr"
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr),
                    "-subj", "/O=release-demo:presenters/CN=presenter"], check=True, capture_output=True)
    key.chmod(0o600)
    request = base64.b64encode(csr.read_bytes()).decode()
    code, output = host.run(f"python3 scripts/demos/kubernetes/host/k3s.py presenter {request}", timeout=120)
    if code != 0:
        fail(f"The cluster didn't sign the request:\n{output}\nIs it up? scripts/demo.sh up kubernetes")
    signed = json.loads(output.strip().splitlines()[-1])
    config = {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "release-demo", "cluster": {
            "server": f"https://127.0.0.1:{min(TUNNELS)}", "certificate-authority-data": signed["ca"]}}],
        "users": [{"name": "presenter", "user": {
            "client-certificate-data": signed["certificate"],
            "client-key-data": base64.b64encode(key.read_bytes()).decode()}}],
        "contexts": [{"name": "release-demo", "context": {"cluster": "release-demo", "user": "presenter",
                                                          "namespace": "production"}}],
        "current-context": "release-demo",
    }
    path = FOLDER / "config"
    path.touch(mode=0o600)
    path.write_text(json.dumps(config, indent=2) + "\n")
    key.unlink()
    csr.unlink()
    return path


def main():
    if not shutil.which("session-manager-plugin"):
        fail("The AWS CLI needs its Session Manager plugin for the tunnels: brew install --cask session-manager-plugin")
    path = certificate()
    instance = host.instance(aws_session())
    env = {**os.environ, "AWS_PROFILE": os.environ.get("AWS_PROFILE", "release-demo"), "AWS_REGION": "eu-west-1"}
    tunnels = [subprocess.Popen(
        ["aws", "ssm", "start-session", "--target", instance, "--document-name", "AWS-StartPortForwardingSession",
         "--parameters", json.dumps({"portNumber": [str(remote)], "localPortNumber": [str(local)]})],
        env=env, stdout=subprocess.DEVNULL) for local, remote in TUNNELS.items()]
    print(f"""Certificate for 8 hours in {path}. Tunnels open until Ctrl-C. In the video's terminal:

  export KUBECONFIG={path}
  SA={SA}
  kubectl auth can-i patch deploy -n staging --as $SA
  kubectl get pods -n production -w -o custom-columns='POD:.metadata.name,VERSION:.metadata.labels.app\\.kubernetes\\.io/version,READY:.status.containerStatuses[0].ready'
  helm history catalog -n production --max 3 -o json | jq -r '.[] | "\\(.revision)  \\(.app_version)  \\(.status)  \\(.description[:24])"'

Production's Service for the prober: http://127.0.0.1:18082""", flush=True)
    signal.signal(signal.SIGINT, lambda *_: [t.terminate() for t in tunnels])
    for tunnel in tunnels:
        tunnel.wait()


if __name__ == "__main__":
    main()
