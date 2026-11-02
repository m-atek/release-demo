"""Puts orders-service 1.12.0 live outside the pipeline (video 24): the one staged fact of the video.

    python3 scripts/demos/patch_clock/seed.py        the last stage of the Jenkins job patch-clock-seed

The gate refuses 1.12.0, so it can't come through the pipeline. Today's search would truthfully answer
"0 of n", so a setup step puts it there. The job stages 1.12.0 in its workspace only, builds it with
toolchain.build, and calls this, which:

1. runs Syft alone, with the pinned image, over the unpacked zip, as the stage's first half does. No Grype;
2. uploads the zip, build-info.json, sbom.cdx.json and sbom-info.json to orders-service-candidates/1.12.0/seed/,
   which the main reset doesn't touch;
3. with the deploy role, updates each function's code, publishes a Lambda version described 1.12.0, moves
   both live aliases;
4. runs check.py for both environments.

No run state, no change, no Slack, no event, and no record that claims a scan passed. Nothing else in the
repository needs this path, and no other demo sees it. The script's description says it plainly.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import publish  # noqa: E402
import tools  # noqa: E402
from common import ROOT, fail, get_parameter, service, ssm_client  # noqa: E402

SERVICE, VERSION = "orders-service", "1.12.0"
SEED = f"{SERVICE}-candidates/{VERSION}/seed"
PACKAGE = f"{SERVICE}-{VERSION}.zip"
FILES = [PACKAGE, "build-info.json", publish.SBOM, publish.SBOM_INFO]


def stored_package(token):
    """The seed's checksum in JFrog, or None before the first seed."""
    return publish.stored_sha256(token, f"{SEED}/{PACKAGE}")


def upload(token, package, info, sbom):
    """Into the seed folder. Its token can't overwrite, so a second, different seed stops here, before anything moves.
    The same package again keeps the files it has: only a later run's build link would differ."""
    existing = stored_package(token)
    if existing and existing != info["sha256"]:
        fail(f"{SEED}/ already holds another package (SHA-256 {existing}). To seed again, delete that folder in "
             "JFrog as admin, then run this job.")
    if existing:
        print(f"{SEED}/ already holds this package")
    else:
        publish.upload(token, f"{SEED}/build-info.json", (json.dumps(info, indent=2) + "\n").encode())
        publish.upload_sbom(token, SEED, info, sbom)
        # The package last: a seed cut short leaves a folder the next run still fills.
        publish.upload(token, f"{SEED}/{PACKAGE}", package)
        print(f"Stored {', '.join(FILES)} in {SEED}/")


def deploy_one(client, function, package, sha256):
    """The function's code, a version described 1.12.0, and live on it. Returns the function URL."""
    from evidence import base64_to_hex

    client.update_function_code(FunctionName=function, ZipFile=package)
    client.get_waiter("function_updated_v2").wait(FunctionName=function)
    published = client.publish_version(FunctionName=function, Description=VERSION)
    if base64_to_hex(published["CodeSha256"]) != sha256:
        fail(f"{function}: version {published['Version']} doesn't hold the seed's package.")
    client.update_alias(FunctionName=function, Name="live", FunctionVersion=published["Version"])
    print(f"{function}: version {published['Version']} ({VERSION}) is live")
    return client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"]


def check(environment, url):
    """check.py, as the pipeline runs it for this environment."""
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py"), SERVICE, environment, "--version", VERSION, "--url", url])
    if done.returncode != 0:
        fail(f"{environment} doesn't pass check.py on {VERSION}. reset.py puts both aliases back on 1.3.0.")


def main():
    from deploy import lambda_client

    out = Path(os.environ.get("WORKSPACE", ROOT)) / "dist" / SERVICE
    info = json.loads((out / "build-info.json").read_text())
    package = (out / PACKAGE).read_bytes()
    if info["version"] != VERSION or hashlib.sha256(package).hexdigest() != info["sha256"]:
        fail(f"dist/{SERVICE} doesn't hold {VERSION} as its build-info.json describes it.")
    with tools.scratch() as work:
        sbom = tools.syft(tools.unpack(package, work), work, SERVICE)
    upload(get_parameter(ssm_client(), "jcr/token"), package, info, sbom)
    client = lambda_client(SERVICE)
    urls = {name: deploy_one(client, environment["function"], package, info["sha256"])
            for name, environment in service(SERVICE)["environments"].items()}
    for name, url in urls.items():
        check(name, url)
    print(f"Seeded: {VERSION} is live in both environments, outside the pipeline. reset.py puts both aliases back on 1.3.0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
