"""What is live, and the SBOM stored with exactly that package. Reads only.

services.yaml's `live:` block says how to read a service: `reader` names a function here,
one small function per kind of target, and a service without the block is listed as not
searched. Then the service's release folder in JFrog: <releases root>/<version>/, or a
<build>/ folder beside it when a version has two builds. The folder whose build-info.json
names the package that is live holds the SBOM to search, and sbom-info.json must tie that SBOM
to that package. That is what makes "the SBOM of what is live" true.
"""

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

from common import REGION, aws_session, get_parameter, services, ssm_client  # noqa: E402
from evidence import base64_to_hex  # noqa: E402
from publish import JCR, SBOM, SBOM_INFO, releases_root  # noqa: E402

READER_ROLE = "release-demo-sbom-reader"
ALIAS = "live"
SEMVER = re.compile(r"\d+\.\d+\.\d+")


class NoAppVersion(Exception):
    """The live Lambda version's description isn't an app version."""


class CanaryRunning(Exception):
    """Two versions take traffic at this moment."""


class JFrogAnswered(Exception):
    """JFrog answered with something other than the file or a plain 404."""


@dataclass(frozen=True)
class Live:
    """What one service runs at this moment."""
    version: str
    lambda_version: str
    sha256: str = ""     # a zip's checksum, as hex
    digest: str = ""     # an image's digest, sha256:<hex>

    def is_named_by(self, info):
        """Does this build-info.json describe this package?"""
        if self.digest:
            return self.digest in ((info.get("image") or {}).get("digest"), info.get("image_digest"))
        return bool(self.sha256) and info.get("sha256") == self.sha256


# Reading what is live: one function per reader in services.yaml.

def lambda_alias(client, function):
    """The live alias of a Lambda function: its version, whose description is the app version, and
    its code's checksum. An image function's CodeSha256 is its digest, as hex; a zip's is base64."""
    alias = client.get_alias(FunctionName=function, Name=ALIAS)
    if (alias.get("RoutingConfig") or {}).get("AdditionalVersionWeights"):
        raise CanaryRunning(function)
    number = alias["FunctionVersion"]
    config = client.get_function_configuration(FunctionName=function, Qualifier=number)
    version = config.get("Description") or ""
    if not SEMVER.fullmatch(version):
        raise NoAppVersion(function)
    if config.get("PackageType") == "Image":
        return Live(version, number, digest=f"sha256:{config['CodeSha256']}")
    return Live(version, number, sha256=base64_to_hex(config["CodeSha256"]))


READERS = {"lambda-alias": lambda_alias}


def read_live(client, block):
    return READERS[block["reader"]](client, block["function"])


def searchable():
    """{service: its live block}, in services.yaml's order."""
    return {name: config["live"] for name, config in services().items() if config.get("live")}


def not_searched():
    """The services without a live block. A service released through pull requests (the network stack) has no package."""
    return sorted(name for name, config in services().items()
                  if not config.get("live") and config.get("pipeline", "release") != "pull-request")


def reader_client(session=None):
    """Lambda as release-demo-sbom-reader, which only the tools host may assume."""
    import boto3

    session = session or aws_session()
    sts = session.client("sts")
    account = sts.get_caller_identity()["Account"]
    credentials = sts.assume_role(RoleArn=f"arn:aws:iam::{account}:role/{READER_ROLE}",
                                  RoleSessionName=f"sbom-search-{int(time.time())}")["Credentials"]
    return boto3.client("lambda", region_name=REGION, aws_access_key_id=credentials["AccessKeyId"],
                        aws_secret_access_key=credentials["SecretAccessKey"], aws_session_token=credentials["SessionToken"])


# JFrog, reading.

class JFrog:
    """JFrog Container Registry with a token: GET, and nothing else."""

    def __init__(self, token, base=JCR):
        self.token, self.base = token, base.rstrip("/")

    def get(self, path):
        """(status, body); a refusal or a missing file is a status, never an exception."""
        request = urllib.request.Request(f"{self.base}/artifactory/{path}", headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()


def evidence_reader():
    """JFrog as evidence-reader: it reads candidates and releases, and deploys nothing."""
    return JFrog(get_parameter(ssm_client(), "jcr/evidence-reader-token"))


def present(jfrog, path):
    """The file's bytes, or None when JFrog has no such file. Any other answer is an error, not a no."""
    status, body = jfrog.get(path)
    if status == 200:
        return body
    if status == 404:
        return None
    raise JFrogAnswered(str(status))


def folders_of(jfrog, root):
    """A version's release folder and the <build>/ folders beside its package."""
    status, body = jfrog.get(f"api/storage/{root}")
    if status == 404:
        return []
    if status != 200:
        raise JFrogAnswered(str(status))
    return [root] + [f"{root}/{child['uri'].strip('/')}" for child in json.loads(body).get("children", []) if child.get("folder")]


def release_folder(jfrog, service_name, live):
    """(folder, its build-info) of the release copy that is live, or (None, [the folders that aren't])."""
    others = []
    for folder in folders_of(jfrog, f"{releases_root(service_name)}/{live.version}"):
        info = present(jfrog, f"{folder}/build-info.json")
        if info is None:
            continue
        info = json.loads(info)
        if live.is_named_by(info):
            return folder, info
        others.append(folder)
    return None, others


def sbom_is_this_package(jfrog, folder, info, sbom):
    """sbom-info.json ties the SBOM to the package. Without it, or with other checksums, it isn't this package's."""
    tied = present(jfrog, f"{folder}/{SBOM_INFO}")
    if tied is None:
        return False
    tied = json.loads(tied)
    return tied.get("sbom_sha256") == hashlib.sha256(sbom).hexdigest() and tied.get("package_sha256") == info.get("sha256")


NONE_STORED, NOT_THIS_PACKAGE = "none stored", "not this package"


def stored_sbom(jfrog, service_name, live):
    """(the SBOM's bytes, None) when one is stored with what is live, else (None, why not)."""
    folder, found = release_folder(jfrog, service_name, live)
    if folder is None:
        # Release copies of this version, none of them what is live. Without an SBOM there is nothing that can mismatch.
        anything = any(present(jfrog, f"{other}/{SBOM}") is not None for other in found)
        return None, NOT_THIS_PACKAGE if anything else NONE_STORED
    sbom = present(jfrog, f"{folder}/{SBOM}")
    if sbom is None:
        return None, NONE_STORED
    if not sbom_is_this_package(jfrog, folder, found, sbom):
        return None, NOT_THIS_PACKAGE
    return sbom, None
