"""The cluster side of the Kubernetes demo: Helm, the build's token and the API.

Shared by deploy.py, check.py and reset.py, which run on the tools host:

- helm is Helm 4, a pinned release from get.helm.sh, checked against its
  SHA-256 and kept in the cache folder;
- credentials() asks the root-owned helper for a kubeconfig with a token for
  the Jenkins service account that lasts 10 minutes (kubectl create token).
  The Jenkins agent runs it through sudo. Nothing long-lived is stored: the
  kubeconfig sits in a private temporary folder and goes when the step ends;
- Api reads the cluster with that token, for the checks and the rollout watch.
"""

import base64
import contextlib
import datetime
import io
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.append(str(Path(__file__).resolve().parents[2]))
from common import ROOT, fail  # noqa: E402

HELM_VERSION = "v4.3.1"
HELM_SHA256 = {
    "linux-amd64": "0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b",
    "darwin-arm64": "6b4e61c8cd27d99e5a801e63ff25dd5c70b36875c2cd659d3e9ce6b76fc23198",
}
TOKEN_HELPER = "/usr/local/sbin/release-demo-kube-token"
SERVICE_ACCOUNT = "system:serviceaccount:jenkins:release-pipeline"


def cache_dir():
    path = Path(os.environ.get("RELEASE_DEMO_CACHE") or ROOT / ".local" / "cache")
    path.mkdir(parents=True, exist_ok=True)
    return path


def helm_binary():
    """$HELM if set (the toolchain image has helm), else the pinned release, downloaded once."""
    if os.environ.get("HELM"):
        return os.environ["HELM"]
    system = platform.system().lower()
    machine = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    target = f"{system}-{machine}"
    binary = cache_dir() / f"helm-{HELM_VERSION}-{target}" / "helm"
    if binary.exists():
        return str(binary)
    url = f"https://get.helm.sh/helm-{HELM_VERSION}-{target}.tar.gz"
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = response.read()
    import hashlib
    actual = hashlib.sha256(archive).hexdigest()
    if actual != HELM_SHA256[target]:
        fail(f"{url} has SHA-256 {actual}, expected {HELM_SHA256[target]}. Not running it.")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        data = tar.extractfile(f"{target}/helm").read()
    binary.parent.mkdir(parents=True, exist_ok=True)
    partial = binary.with_name(f"helm.{os.getpid()}.partial")
    partial.write_bytes(data)
    partial.chmod(0o755)
    partial.replace(binary)
    print(f"Helm {HELM_VERSION} from get.helm.sh, SHA-256 verified")
    return str(binary)


def token_claims(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@contextlib.contextmanager
def credentials(quiet=False):
    """A kubeconfig path for the Jenkins service account, valid 10 minutes, removed afterwards."""
    helper = os.environ.get("KUBE_TOKEN_HELPER", TOKEN_HELPER)
    command = [helper] if os.geteuid() == 0 else ["sudo", "-n", helper]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"The token helper failed: {result.stderr.strip() or result.returncode}. Is the cluster up? "
             "scripts/demo.sh up kubernetes")
    folder = Path(tempfile.mkdtemp(prefix="kube-"))
    try:
        folder.chmod(0o700)
        path = folder / "kubeconfig"
        path.touch(mode=0o600)
        path.write_text(result.stdout)
        config = yaml.safe_load(result.stdout)
        claims = token_claims(config["users"][0]["user"]["token"])
        if not quiet:
            for line in result.stderr.strip().splitlines():
                print(line)
            until = datetime.datetime.fromtimestamp(claims["exp"], datetime.timezone.utc)
            minutes = (claims["exp"] - claims["iat"]) // 60
            print(f"Token for {claims['sub']}: valid {minutes} minutes, until {until:%H:%M:%S} UTC. Not stored.")
        yield path
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class Api:
    """GET requests to the Kubernetes API with the kubeconfig's token and CA."""

    def __init__(self, kubeconfig):
        config = yaml.safe_load(Path(kubeconfig).read_text())
        cluster = config["clusters"][0]["cluster"]
        self.server = cluster["server"].rstrip("/")
        self.token = config["users"][0]["user"]["token"]
        self.context = ssl.create_default_context(
            cadata=base64.b64decode(cluster["certificate-authority-data"]).decode())

    def get(self, path):
        request = urllib.request.Request(self.server + path, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=15, context=self.context) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def pods(self, namespace, release):
        reply = self.get(f"/api/v1/namespaces/{namespace}/pods?labelSelector=app.kubernetes.io/instance%3D{release}")
        return reply["items"] if reply else []

    def deployment(self, namespace, name):
        return self.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")


def pod_summary(pod):
    """(version label, ready, deleting, image digest or None) of one pod."""
    statuses = pod.get("status", {}).get("containerStatuses") or [{}]
    image_id = statuses[0].get("imageID", "")
    return (pod["metadata"]["labels"].get("app.kubernetes.io/version", "?"), bool(statuses[0].get("ready")),
            bool(pod["metadata"].get("deletionTimestamp")), image_id.split("@")[-1] if "@" in image_id else None)


def helm(kubeconfig, *args, cwd=None):
    """Run helm with this kubeconfig and its own state folders, so it writes nothing to $HOME."""
    home = Path(kubeconfig).parent
    env = {**os.environ, "KUBECONFIG": str(kubeconfig), "HELM_CACHE_HOME": str(home / "helm-cache"),
           "HELM_CONFIG_HOME": str(home / "helm-config"), "HELM_DATA_HOME": str(home / "helm-data")}
    return subprocess.run([helm_binary(), *args], env=env, cwd=cwd, capture_output=True, text=True)


def history(kubeconfig, release, namespace):
    """helm history as a list, oldest first, or [] before the first install."""
    result = helm(kubeconfig, "history", release, "--namespace", namespace, "--max", "20", "--output", "json")
    if result.returncode != 0:
        if "not found" in result.stderr:
            return []
        fail(f"helm history {release} in {namespace} failed: {result.stderr.strip()}")
    return sorted(json.loads(result.stdout or "[]"), key=lambda r: r["revision"])


def deployed(revisions):
    """The revision Helm calls deployed, or None."""
    current = [r for r in revisions if r["status"] == "deployed"]
    return current[-1] if current else None


def minutes(text):
    """3m, 180s or 1m30s in words: "3 minutes"."""
    seconds = parse_duration(text)
    if seconds % 60 == 0:
        count = seconds // 60
        return f"{count} minute{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


def parse_duration(text):
    total, number = 0, ""
    for char in text:
        if char.isdigit():
            number += char
        else:
            total += int(number) * {"h": 3600, "m": 60, "s": 1}[char]
            number = ""
    return total + (int(number) if number else 0)


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def now():
    return time.monotonic()
