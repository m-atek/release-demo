"""Put the Apple signing and notarization secrets in SSM, for STANDIN_APPLE=0.

    python scripts/demos/desktop/apple_setup.py --application DeveloperIDApplication.p12 \\
        --installer DeveloperIDInstaller.p12 --notary-key AuthKey_ABC123.p8 --key-id ABC123

Export both Developer ID certificates with their keys from Keychain Access as
.p12 files, each with a passphrase, and download the App Store Connect API
key (Users and Access, Integrations, Team Keys, with the Developer role). This
asks for both passphrases and the Issuer ID without echo, checks that each
.p12 opens with its passphrase, and stores everything as SecureString under
apple/ in SSM, in base64. A .p12 is larger than a standard parameter holds,
so those two are advanced parameters (USD 0.05 a month each). The tools host
hands them to Jenkins as credentials for the Mac agent's build. Delete the
exported files afterwards.
"""

import argparse
import base64
import getpass
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from common import SSM_PREFIX, fail, ssm_client  # noqa: E402


def opens(p12, passphrase):
    """Does the .p12 open with this passphrase? openssl reads it from the environment, not the command line."""
    env = {**os.environ, "P12_PASSPHRASE": passphrase}
    for extra in ([], ["-legacy"]):
        result = subprocess.run(["openssl", "pkcs12", "-in", str(p12), "-nokeys", "-passin", "env:P12_PASSPHRASE", *extra],
                                env=env, capture_output=True, text=True)
        if result.returncode == 0:
            return "Developer ID" in result.stdout
    return False


def store(ssm, name, value, secure=True):
    tier = "Advanced" if len(value) > 4096 else "Standard"
    ssm.put_parameter(Name=SSM_PREFIX + name, Value=value, Overwrite=True, Tier=tier,
                      Type="SecureString" if secure else "String")
    print(f"  {name}: saved ({tier.lower()})")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--application", type=Path, required=True, help="Developer ID Application .p12")
    parser.add_argument("--installer", type=Path, required=True, help="Developer ID Installer .p12")
    parser.add_argument("--notary-key", type=Path, required=True, help="App Store Connect API key, AuthKey_<id>.p8")
    parser.add_argument("--key-id", required=True, help="that key's Key ID")
    args = parser.parse_args()

    ssm = ssm_client()
    for kind, path in (("application", args.application), ("installer", args.installer)):
        passphrase = getpass.getpass(f"Passphrase of {path.name} (hidden): ")
        if not opens(path, passphrase):
            fail(f"{path.name} doesn't open with that passphrase, or holds no Developer ID certificate.")
        store(ssm, f"apple/developer-id-{kind}-p12", base64.b64encode(path.read_bytes()).decode())
        store(ssm, f"apple/developer-id-{kind}-password", passphrase)
    key = args.notary_key.read_text()
    if "BEGIN PRIVATE KEY" not in key:
        fail(f"{args.notary_key.name} isn't an App Store Connect API key (.p8).")
    store(ssm, "apple/notary-key", base64.b64encode(key.encode()).decode())
    store(ssm, "apple/notary-key-id", args.key_id, secure=False)
    issuer = getpass.getpass("Issuer ID (App Store Connect, Users and Access, Integrations; hidden): ").strip()
    store(ssm, "apple/notary-issuer", issuer)
    print("Done. Set STANDIN_APPLE: \"0\" for store-assistant in services.yaml to use them.")


if __name__ == "__main__":
    main()
