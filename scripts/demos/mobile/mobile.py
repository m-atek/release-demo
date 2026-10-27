"""What demo 7's scripts share: the store-floor settings, which outside
services run as stand-ins, the place stand-ins keep their state, and reading
the .ipa and .aab files the stores receive.

Imported by the scripts next to it. They run from the repository root, like
every stage script, so this file puts scripts/ on the import path.
"""

import hashlib
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
# Appended, not prepended: scripts/secrets.py must not hide the standard library's secrets.
sys.path.append(str(ROOT / "scripts"))

from common import aws_session, fail, get_parameter, service, ssm_client  # noqa: E402

SERVICE = "store-floor"
STANDINS = ("apple", "google-play", "jamf")


def config():
    return service(SERVICE)


def standins(settings=None):
    """The outside services that run as stand-ins: STANDIN in the service's
    settings is "1" for all of them, "0" for none, or a list such as "jamf,apple".
    The environment variable STANDIN overrides it, for local runs."""
    value = os.environ.get("STANDIN")
    if value is None:
        value = str((settings if settings is not None else config().get("settings", {})).get("STANDIN", "1"))
    value = value.strip().lower()
    if value in ("1", "true", "yes", "all"):
        return set(STANDINS)
    if value in ("", "0", "false", "no", "none"):
        return set()
    chosen = {name.strip() for name in value.split(",") if name.strip()}
    unknown = chosen - set(STANDINS)
    if unknown:
        fail(f"STANDIN names {', '.join(sorted(unknown))}: the stand-ins are {', '.join(STANDINS)}")
    return chosen


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- Where stand-ins keep their state --------------------------------------


class LocalStore:
    """Stand-in state in a local folder: for tests and runs without AWS."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def read(self, key):
        path = self.directory / key
        return json.loads(path.read_text()) if path.exists() else None

    def write(self, key, data):
        path = self.directory / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")

    def where(self):
        return str(self.directory)


class S3Store:
    """Stand-in state in the demo's bucket, under standins/. The tools host
    reaches it through the deploy role; on the Mac, your own sign-in does."""

    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def read(self, key):
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=f"standins/{key}")["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None
        return json.loads(body)

    def write(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=f"standins/{key}", ContentType="application/json",
                               Body=(json.dumps(data, indent=2) + "\n").encode())

    def where(self):
        return "the demo's bucket"


def deploy_session():
    """The store-floor deploy role on the tools host, which may only touch this
    demo's bucket. Elsewhere (your Mac), your own short-lived sign-in."""
    session = aws_session()
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    if ":assumed-role/release-demo-tools-host/" not in identity["Arn"]:
        return session
    role = f"arn:aws:iam::{identity['Account']}:role/release-demo-deploy-{SERVICE}"
    creds = sts.assume_role(RoleArn=role, RoleSessionName=f"store-floor-{int(time.time())}")["Credentials"]
    import boto3

    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=session.region_name)


def state_store():
    """MOBILE_STANDIN_DIR names a local folder; otherwise the demo's S3 bucket."""
    if os.environ.get("MOBILE_STANDIN_DIR"):
        return LocalStore(os.environ["MOBILE_STANDIN_DIR"])
    bucket = get_parameter(ssm_client(), f"{SERVICE}/bucket")
    return S3Store(deploy_session().client("s3"), bucket)


# --- What the stores receive ------------------------------------------------


def ipa_info(data):
    """The app inside an .ipa: Info.plist's identity and versions, how it was
    signed, and its provisioning profile's method where there is one."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        plists = [n for n in names if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if not plists:
            raise ValueError("not an iOS app: no Payload/<name>.app/Info.plist")
        app = plists[0].rsplit("/", 1)[0]
        info = plistlib.loads(archive.read(plists[0]))
        profile = {}
        if f"{app}/embedded.mobileprovision" in names:
            profile = provisioning_profile(archive.read(f"{app}/embedded.mobileprovision"))
        export = {}
        if f"{app}/StandinExport.plist" in names:
            export = plistlib.loads(archive.read(f"{app}/StandinExport.plist"))
        return {
            "bundle_id": info.get("CFBundleIdentifier"),
            "version": info.get("CFBundleShortVersionString"),
            "build": info.get("CFBundleVersion"),
            "name": info.get("CFBundleDisplayName") or info.get("CFBundleName"),
            "encryption_declared": "ITSAppUsesNonExemptEncryption" in info,
            "uses_encryption": info.get("ITSAppUsesNonExemptEncryption"),
            "signed": f"{app}/_CodeSignature/CodeResources" in names,
            "method": export.get("method") or profile_method(profile),
            "devices": profile.get("ProvisionedDevices", export.get("devices", [])),
            "size_bytes": len(data),
        }


def provisioning_profile(data):
    """The plist inside a CMS-signed provisioning profile, without verifying it."""
    start, end = data.find(b"<?xml"), data.find(b"</plist>")
    return plistlib.loads(data[start:end + len(b"</plist>")]) if start >= 0 and end > start else {}


def profile_method(profile):
    if not profile:
        return None
    if profile.get("ProvisionsAllDevices"):
        return "enterprise"
    if profile.get("ProvisionedDevices"):
        return "ad-hoc" if not profile.get("Entitlements", {}).get("get-task-allow") else "development"
    return "app-store-connect"


def aab_info(data):
    """The package name, version, target SDK and upload certificate of an
    Android App Bundle, read from its protobuf manifest and its JAR signature."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        manifest = parse_manifest(archive.read("base/manifest/AndroidManifest.xml"))
        signature = next((n for n in names if re.fullmatch(r"META-INF/[^/]+\.(RSA|EC|DSA)", n)), None)
        certificate = signer_sha256(archive.read(signature)) if signature else None
    return {**manifest, "signed": certificate is not None, "upload_certificate_sha256": certificate,
            "size_bytes": len(data)}


def signer_sha256(pkcs7):
    """SHA-256 fingerprint of the certificate in a JAR signature block, through openssl."""
    with tempfile.TemporaryDirectory() as temp:
        block = Path(temp) / "signature"
        block.write_bytes(pkcs7)
        pem = subprocess.run(["openssl", "pkcs7", "-inform", "DER", "-in", str(block), "-print_certs"],
                             capture_output=True, text=True, check=True).stdout
        der = subprocess.run(["openssl", "x509", "-outform", "DER"], input=pem.encode(),
                             capture_output=True, check=True).stdout
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def _fields(data):
    """Protobuf fields as (number, wire type, value): just enough for aapt2's XML."""
    position = 0
    while position < len(data):
        key, position = _varint(data, position)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, position = _varint(data, position)
        elif wire == 2:
            length, position = _varint(data, position)
            value, position = data[position:position + length], position + length
        elif wire == 5:
            value, position = data[position:position + 4], position + 4
        elif wire == 1:
            value, position = data[position:position + 8], position + 8
        else:
            raise ValueError(f"unexpected protobuf wire type {wire}")
        yield number, wire, value


def _varint(data, position):
    result = shift = 0
    while True:
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7


def _element(node):
    """XmlNode.element (field 1) as (name, {attribute: value}, children)."""
    for number, _, value in _fields(node):
        if number == 1:
            name, attributes, children = "", {}, []
            for field, _, content in _fields(value):
                if field == 3:
                    name = content.decode()
                elif field == 4:
                    attribute = dict((n, v) for n, _, v in _fields(content))
                    attr_name = attribute.get(2, b"").decode()
                    attr_value = attribute.get(3, b"").decode()
                    if not attr_value and 6 in attribute:
                        attr_value = _primitive(attribute[6])
                    attributes[attr_name] = attr_value
                elif field == 5:
                    children.append(content)
            return name, attributes, children
    return None, {}, []


def _primitive(item):
    for number, _, value in _fields(item):
        if number == 7:  # Primitive
            for kind, _, content in _fields(value):
                if kind in (6, 7):  # int_decimal_value, int_hexadecimal_value
                    return str(content)
    return ""


def parse_manifest(data):
    name, attributes, children = _element(data)
    if name != "manifest":
        raise ValueError("not an app bundle manifest")
    info = {"package": attributes.get("package"), "version_code": int(attributes.get("versionCode") or 0),
            "version_name": attributes.get("versionName"), "target_sdk": None, "min_sdk": None}
    for child in children:
        child_name, child_attributes, _ = _element(child)
        if child_name == "uses-sdk":
            info["target_sdk"] = int(child_attributes.get("targetSdkVersion") or 0) or None
            info["min_sdk"] = int(child_attributes.get("minSdkVersion") or 0) or None
    return info


if __name__ == "__main__":
    # python3 scripts/demos/mobile/mobile.py standin apple: "yes" when that service is a stand-in.
    if len(sys.argv) == 3 and sys.argv[1] == "standin":
        print("yes" if sys.argv[2] in standins() else "no")
