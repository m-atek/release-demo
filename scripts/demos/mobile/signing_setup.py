"""Put store-floor's signing keys in SSM, without showing them to anyone.

    python scripts/demos/mobile/signing_setup.py android-upload-key        make the upload key, once
    python scripts/demos/mobile/signing_setup.py app-store-connect-key ~/Downloads/AuthKey_ABC123.p8
    python scripts/demos/mobile/signing_setup.py play-service-account-key ~/Downloads/key.json

android-upload-key makes the Android upload key in a temporary folder (RSA
2048, PKCS12, 25 years) with a random password, stores both in SSM, deletes
the folder, and prints only the certificate's fingerprint. Google keeps the
app signing key (Play App Signing): the first bundle uploaded with this key
makes it the app's upload key. It refuses to replace a key that is set,
because replacing an upload key means asking Google to reset it.

The other two read a key file you downloaded, check it looks right, store it,
and tell you to delete the file. Values go only to SSM: never to the screen, a
log or the repository. After a change, the tools host loads them into
Jenkins at its next start (scripts/tools.sh down, then up).
"""

import argparse
import base64
import json
import os
import secrets
import tempfile
from pathlib import Path

import mobile
from mobile import SERVICE, fail

from common import put_parameter  # noqa: E402

KEYSTORE, PASSWORD = f"{SERVICE}/android-upload-keystore", f"{SERVICE}/android-upload-key-password"


def is_set(ssm, name):
    try:
        return ssm.get_parameter(Name=mobile.SSM_PREFIX + name)["Parameter"]["Version"] > 1
    except ssm.exceptions.ParameterNotFound:
        fail(f"{mobile.SSM_PREFIX}{name} doesn't exist yet. Apply infra/ first: scripts/tf.sh apply")


def android_upload_key(ssm, replace):
    if is_set(ssm, KEYSTORE) and not replace:
        fail("The upload key is set already. Replacing it needs an upload key reset in the Play Console first; "
             "then run again with --replace.")
    import flutter

    password = secrets.token_urlsafe(24)
    # keytool from the Flutter container's JDK, in a folder under .local/ (which
    # git ignores) that the container can see; the password goes by name only.
    (mobile.ROOT / ".local").mkdir(exist_ok=True)
    os.environ["UPLOAD_KEY_PASSWORD"] = password
    try:
        with tempfile.TemporaryDirectory(dir=mobile.ROOT / ".local") as temp:
            keystore = Path(temp) / "upload.p12"
            options = "-storetype PKCS12 -keystore upload.p12 -storepass:env UPLOAD_KEY_PASSWORD"
            flutter.in_container(f"keytool -genkeypair {options} -alias upload -keyalg RSA -keysize 2048 -validity 9125 "
                                 "-dname 'CN=Store Floor upload key, O=Expeditious Software, C=NL' >/dev/null 2>&1",
                                 cwd=temp, env_names=["UPLOAD_KEY_PASSWORD"])
            listing = flutter.in_container(f"keytool -list -v {options}", cwd=temp, env_names=["UPLOAD_KEY_PASSWORD"],
                                           capture=True).stdout
            encoded = base64.b64encode(keystore.read_bytes()).decode()
    finally:
        del os.environ["UPLOAD_KEY_PASSWORD"]
    if len(encoded) > 4096:
        fail("The keystore is larger than SSM's 4 KB standard parameter. Nothing stored.")
    put_parameter(ssm, KEYSTORE, encoded)
    put_parameter(ssm, PASSWORD, password)
    fingerprint = next((line.split(":", 1)[1].strip() for line in listing.splitlines() if "SHA256:" in line), "?")
    print(f"Stored the upload key and its password in SSM ({KEYSTORE}, {PASSWORD}).")
    print(f"Upload certificate SHA-256: {fingerprint}")


def app_store_connect_key(ssm, path):
    text = Path(path).expanduser().read_text()
    if "-----BEGIN PRIVATE KEY-----" not in text:
        fail(f"{path} isn't an App Store Connect API key (.p8).")
    put_parameter(ssm, f"{SERVICE}/asc-private-key", text.strip() + "\n")
    key_id = Path(path).stem.removeprefix("AuthKey_")
    put_parameter(ssm, f"{SERVICE}/asc-key-id", key_id)
    print(f"Stored the App Store Connect API key {key_id} in SSM. Delete {path} now.")
    print("Then set its issuer id: python scripts/secrets.py set store-floor/asc-issuer-id")


def play_service_account_key(ssm, path):
    key = json.loads(Path(path).expanduser().read_text())
    if key.get("type") != "service_account" or "private_key" not in key:
        fail(f"{path} isn't a Google service account key.")
    put_parameter(ssm, f"{SERVICE}/play-service-account-json", json.dumps(key))
    print(f"Stored the key of {key.get('client_email')} in SSM. Delete {path} now.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    upload = sub.add_parser("android-upload-key")
    upload.add_argument("--replace", action="store_true")
    sub.add_parser("app-store-connect-key").add_argument("path")
    sub.add_parser("play-service-account-key").add_argument("path")
    args = parser.parse_args()

    ssm = mobile.ssm_client()
    if args.command == "android-upload-key":
        android_upload_key(ssm, args.replace)
    elif args.command == "app-store-connect-key":
        app_store_connect_key(ssm, args.path)
    else:
        play_service_account_key(ssm, args.path)


if __name__ == "__main__":
    main()
