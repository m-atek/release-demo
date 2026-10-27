"""Put quote-service 1.1.0, the version every target starts on, in a cloud's registry.

    python scripts/demos/clouds/seed.py aws      build 1.1.0 from demo-states and push it to ECR
    python scripts/demos/clouds/seed.py azure    copy it by digest from ECR to the Azure registry
    python scripts/demos/clouds/seed.py gcp      copy it by digest from ECR to Artifact Registry

Terraform creates each cloud's functions or apps on this image, so it must be
in the registry first: bootstrap.sh and up.sh run this between the registry
and the rest. The image is built from demo-states/quote-service/1.1.0 with the
commit "initial", so it is the same image every time, and is tagged 1.1.0,
which ECR's lifecycle rule keeps for reset.py. Runs on the Mac with your own
logins.
"""

import argparse
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers

from build import build_image  # noqa: E402
from common import ROOT, aws_session, fail, service  # noqa: E402
from identity import acr_password, azure, gcp  # noqa: E402
from registry import Layout, Registry, copy  # noqa: E402
from targets import cloud_settings  # noqa: E402

VERSION = "1.1.0"


def ecr():
    session = aws_session()
    account = session.client("sts").get_caller_identity()["Account"]
    reply = session.client("ecr").get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(reply["authorizationToken"]).decode().split(":", 1)
    return Registry(f"{account}.dkr.ecr.eu-west-1.amazonaws.com", user, password, name="Amazon ECR"), "quote-service"


def seed_aws(config):
    registry, repository = ecr()
    with tempfile.TemporaryDirectory() as temp:
        digest, _ = build_image(config, ROOT / "demo-states" / config["name"] / VERSION, VERSION, "initial",
                                "1970-01-01T00:00:00Z", Path(temp))
        copy(Layout(temp), "", digest, registry, repository, VERSION)
    print(f"ECR holds {config['name']}:{VERSION} as {digest}")


def seed_from_ecr(config, cloud):
    source, source_repository = ecr()
    digest = source.digest(source_repository, VERSION)
    if not digest:
        fail(f"ECR has no {config['name']}:{VERSION} yet. First: scripts/demos/clouds/seed.py aws")
    settings = cloud_settings(cloud, config)
    if not settings.get("registry"):
        fail(f"The {cloud} registry doesn't exist yet: apply its root with apps=true first.")
    if cloud == "azure":
        login = azure(settings, None)
        target = Registry(settings["registry"], "00000000-0000-0000-0000-000000000000",
                          acr_password(login, settings["registry"]), name="Azure Container Registry")
    else:
        login = gcp(settings, None)
        target = Registry(settings["registry"], "oauth2accesstoken", login.access["token"], name="Artifact Registry")
    copy(source, source_repository, digest, target, settings["repository"], VERSION)
    print(json.dumps({"digest": digest}))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cloud", choices=["aws", "azure", "gcp"])
    args = parser.parse_args()
    config = service("quote-service")
    seed_aws(config) if args.cloud == "aws" else seed_from_ecr(config, args.cloud)


if __name__ == "__main__":
    main()
