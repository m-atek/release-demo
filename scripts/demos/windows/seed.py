"""Put the fleet's baseline, store-sync 3.2.0, in JCR. Once; up runs it when it's missing.

    python scripts/demos/windows/seed.py              build and upload, unless JCR has it
    python scripts/demos/windows/seed.py --check      only say whether JCR has it

The fleet starts on the release before ORD-31, so JCR must hold it before the
first up: demos-releases/store-sync/3.2.0/. This builds
demo-states/store-sync/3.2.0 in the .NET toolchain container with Docker on
this Mac, checks the checksum against the one services.yaml pins (the build is
reproducible), and uploads the zip and build-info.json through the tools host
as release-pipeline, like publish.py.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, fail, service  # noqa: E402
from host import run_python  # noqa: E402

TOOLCHAIN = "jenkins/toolchain/dotnet.Dockerfile"
IMAGE = "release-demo/dotnet-tools:local"

# Runs on the tools host: uploads the files next to it with release-pipeline's
# token, or with --check says whether the package is there. Prints no token.
UPLOAD = r'''
import hashlib, json, sys, urllib.error, urllib.request, boto3
folder, names, check = sys.argv[1], sys.argv[2:], False
if names and names[0] == "--check":
    check, names = True, names[1:]
token = boto3.client("ssm", region_name="eu-west-1").get_parameter(
    Name="/release-demo/jcr/token", WithDecryption=True)["Parameter"]["Value"]
def call(method, path, data=None, headers=None):
    request = urllib.request.Request("http://localhost:8082/artifactory/" + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + token, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
if check:
    status, body = call("GET", "api/storage/" + folder + "/" + names[0])
    print(json.loads(body).get("checksums", {}).get("sha256") if status == 200 else "missing")
    sys.exit(0)
for name in names:
    content = open(name, "rb").read()
    status, body = call("PUT", folder + "/" + name, content, {
        "X-Checksum-Sha256": hashlib.sha256(content).hexdigest(), "X-Checksum-Sha1": hashlib.sha1(content).hexdigest(),
        "Content-Type": "application/octet-stream"})
    print(folder + "/" + name + ": " + str(status))
    if status not in (200, 201):
        print(body[:300].decode(errors="replace"), file=sys.stderr)
        sys.exit(1)
'''


def stored_sha256(folder, package):
    code, out, err = run_python(UPLOAD, "store-sync: is the baseline in JCR?", args=[folder, "--check", package])
    if code != 0:
        fail(f"Couldn't ask JCR: {err.strip()[-300:]}")
    answer = out.strip().splitlines()[-1] if out.strip() else "missing"
    return None if answer == "missing" else answer


def build(source, out):
    """The baseline from its demo state, in the toolchain container."""
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, "-f", str(ROOT / TOOLCHAIN), str(ROOT / "jenkins/toolchain")],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{ROOT}:/work:ro", "-v", f"{out}:/out", "-w", "/work", IMAGE,
                    "python3", "scripts/demos/windows/build.py", "store-sync", "--source", str(source.relative_to(ROOT)),
                    "--out", "/out"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only say whether JCR has the baseline")
    args = parser.parse_args()

    config = service("store-sync")
    baseline = config["environments"]["production"]["baseline"]
    folder, package = baseline["path"].rsplit("/", 1)

    stored = stored_sha256(folder, package)
    if stored == baseline["sha256"]:
        print(f"JCR has {baseline['path']}, SHA-256 {stored}")
        return
    if stored:
        fail(f"JCR has {baseline['path']} with SHA-256 {stored}, but services.yaml pins {baseline['sha256']}.")
    if args.check:
        fail(f"JCR has no {baseline['path']} yet. Run: python scripts/demos/windows/seed.py")

    with tempfile.TemporaryDirectory() as out:
        build(ROOT / "demo-states" / "store-sync" / baseline["version"], out)
        built = Path(out) / "store-sync"
        info = json.loads((built / "build-info.json").read_text())
        if info["sha256"] != baseline["sha256"]:
            fail(f"The build of {baseline['version']} has SHA-256 {info['sha256']}, services.yaml pins "
                 f"{baseline['sha256']}. If demo-states/store-sync/{baseline['version']} changed on purpose, update the pin.")
        files = {package: (built / package).read_bytes(), "build-info.json": (built / "build-info.json").read_bytes()}
        code, out_text, err = run_python(UPLOAD, f"store-sync: upload the baseline {baseline['version']} to JCR",
                                         args=[folder, *files], files=files)
    print(out_text.strip())
    if code != 0:
        fail(f"The upload failed: {err.strip()[-300:]}")
    print(f"JCR has {baseline['path']}, SHA-256 {baseline['sha256']}")


if __name__ == "__main__":
    main()
