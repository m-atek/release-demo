"""Publish Jenkins's OpenID Connect discovery files where the clouds can read them.

    python scripts/demos/clouds/oidc_issuer.py publish     on the tools host, after Jenkins starts (boot.sh)
    python scripts/demos/clouds/oidc_issuer.py show        what is published, and whether Jenkins agrees

Jenkins sits behind Cloudflare Access, so AWS, Azure and Google Cloud can't
fetch its keys from Jenkins itself. Its ID token credentials name an alternate
issuer instead: a public S3 bucket holding two files and nothing else,
.well-known/openid-configuration and jwks. Jenkins still signs every token;
the bucket only tells the clouds which public keys to trust.

Each ID token credential has its own key pair, and Jenkins makes new ones
whenever it applies casc.yaml: at every start and every configuration reload.
publish asks Jenkins (as admin, on localhost) for the current public key of
every ID token credential that names this issuer, and uploads the key set when
it differs from the published one. On the tools host it writes through the
role release-demo-deploy-oidc-issuer, which may change only those two files.

The issuer URL is in SSM, jenkins/oidc-issuer, written by Terraform in
infra/demos/5-three-clouds. Demos 6 and 7 use the same issuer: their
credentials are found and published the same way.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers

from common import aws_session, fail, get_parameter, ssm_client  # noqa: E402

JENKINS = os.environ.get("JENKINS_LOCAL_URL", "http://localhost:8080")
HOST_SECRET = Path("/run/release-demo/secrets/jenkins-admin-password")
DESCRIPTOR = "descriptorByName/io.jenkins.plugins.oidc_provider.IdTokenStringCredentials"
TYPES = ("OpenID Connect id token", "OpenID Connect id token as file")
PUBLISHER_ROLE = "release-demo-deploy-oidc-issuer"
FILES = (".well-known/openid-configuration", "jwks")


def jenkins_get(path, password):
    auth = base64.b64encode(f"admin:{password}".encode()).decode()
    request = urllib.request.Request(f"{JENKINS}/{path}", headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except urllib.error.URLError as error:
        fail(f"Jenkins doesn't answer at {JENKINS}: {error.reason}")


def admin_password(ssm):
    if os.environ.get("JENKINS_ADMIN_PASSWORD_FILE"):
        return Path(os.environ["JENKINS_ADMIN_PASSWORD_FILE"]).read_text().strip()
    if HOST_SECRET.exists():
        return HOST_SECRET.read_text().strip()
    return get_parameter(ssm, "jenkins/admin-password")


def from_jenkins(issuer, password):
    """(openid-configuration, key set) as Jenkins has them now, for every ID token
    credential that names this issuer. Credentials with another issuer are skipped."""
    status, body = jenkins_get("manage/credentials/store/system/domain/_/api/json?tree=credentials[id,typeName]", password)
    if status != 200:
        fail(f"Jenkins didn't list its credentials ({status})")
    ids = [c["id"] for c in json.loads(body)["credentials"] if c.get("typeName") in TYPES]
    query = urllib.parse.quote(issuer, safe="")
    keys = []
    for credential in ids:
        status, body = jenkins_get(f"{DESCRIPTOR}/jwks?id={urllib.parse.quote(credential)}&issuer={query}", password)
        if status == 200:
            keys += json.loads(body)["keys"]
        elif status != 404:
            fail(f"Jenkins didn't give the key of {credential} ({status})")
    status, body = jenkins_get(f"{DESCRIPTOR}/wellKnownOpenidConfiguration?issuer={query}", password)
    if status != 200:
        fail(f"Jenkins didn't give the discovery document ({status}). Is the OpenID Connect Provider plugin installed?")
    return json.loads(body), {"keys": sorted(keys, key=lambda k: k["kid"])}


def published(issuer):
    """What the clouds see now: the two public files, or None where missing."""
    found = []
    for name in FILES:
        try:
            with urllib.request.urlopen(f"{issuer}/{name}", timeout=15) as response:
                found.append(json.loads(response.read()))
        except (urllib.error.URLError, ValueError):
            found.append(None)
    return found


def s3_client():
    """The publisher role on the tools host; your own profile on the Mac."""
    session = aws_session()
    caller = session.client("sts").get_caller_identity()
    if ":assumed-role/release-demo-tools-host/" not in caller["Arn"]:
        return session.client("s3")
    role = f"arn:aws:iam::{caller['Account']}:role/{PUBLISHER_ROLE}"
    creds = session.client("sts").assume_role(RoleArn=role, RoleSessionName=f"oidc-issuer-{int(time.time())}")["Credentials"]
    return session.client("s3", aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                          aws_session_token=creds["SessionToken"])


def bucket_of(issuer):
    """https://<bucket>.s3.eu-west-1.amazonaws.com to <bucket>."""
    return urllib.parse.urlsplit(issuer).netloc.split(".s3.")[0]


def publish(issuer, configuration, key_set):
    s3 = s3_client()
    for name, document in zip(FILES, (configuration, key_set)):
        s3.put_object(Bucket=bucket_of(issuer), Key=name, Body=json.dumps(document, indent=2).encode(),
                      ContentType="application/json", CacheControl="max-age=60")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["publish", "show"])
    args = parser.parse_args()

    ssm = ssm_client()
    issuer = get_parameter(ssm, "jenkins/oidc-issuer")
    current = published(issuer)
    if args.command == "show":
        keys = [k["kid"] for k in (current[1] or {}).get("keys", [])]
        print(f"Issuer {issuer}")
        print(f"  discovery document: {'published' if current[0] else 'missing'}")
        print(f"  keys: {', '.join(keys) if keys else 'none'}")
        return

    configuration, key_set = from_jenkins(issuer, admin_password(ssm))
    if not key_set["keys"]:
        print(f"No Jenkins credential names the issuer {issuer} yet: nothing to publish.")
        return
    kids = ", ".join(k["kid"] for k in key_set["keys"])
    if current == [configuration, key_set]:
        print(f"Issuer {issuer}: keys of {kids} already published")
        return
    publish(issuer, configuration, key_set)
    print(f"Issuer {issuer}: published the keys of {kids}")


if __name__ == "__main__":
    main()
