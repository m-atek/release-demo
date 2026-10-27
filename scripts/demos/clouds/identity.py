"""Signing in to AWS, Azure and Google Cloud with no stored cloud keys.

In Jenkins, the OpenID Connect Provider plugin mints an ID token for the build
when the pipeline binds a credential (oidc-aws, oidc-azure, oidc-gcp: see
jenkins_credentials in services.yaml), one per cloud, each with that cloud's
audience. Each cloud trusts the token's issuer and trades it for its own
short-lived access:

- AWS: STS AssumeRoleWithWebIdentity, a one-hour session in the deploy role;
- Azure: Microsoft Entra's client-credentials flow with the token as the
  assertion, for the managed identity with the federated credential;
- Google Cloud: Security Token Service, then a one-hour token for the deploy
  service account.

On the Mac, with no ID token, the same functions use your own sign-ins (the
release-demo AWS profile, az login, gcloud auth login), for bootstrap, up and
reset.py.
"""

import base64
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

from common import aws_session, fail  # noqa: E402

TOKEN_VARIABLES = {"aws": "OIDC_AWS_TOKEN", "azure": "OIDC_AZURE_TOKEN", "gcp": "OIDC_GCP_TOKEN"}
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
ARM_SCOPE = "https://management.azure.com/.default"
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class SignInError(Exception):
    pass


def id_token(cloud):
    """The build's ID token for a cloud: OIDC_<CLOUD>_TOKEN, or a file named by OIDC_<CLOUD>_TOKEN_FILE."""
    variable = TOKEN_VARIABLES[cloud]
    if os.environ.get(variable):
        return os.environ[variable].strip()
    if os.environ.get(variable + "_FILE"):
        return Path(os.environ[variable + "_FILE"]).read_text().strip()
    return None


def in_jenkins():
    return bool(os.environ.get("JENKINS_URL") or os.environ.get("BUILD_URL"))


def b64decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def claims(token):
    """The token's claims, unverified: for showing what it says. The clouds verify it."""
    try:
        return json.loads(b64decode(token.split(".")[1]))
    except (IndexError, ValueError):
        raise SignInError("The ID token isn't a JWT") from None


def http(method, url, data=None, headers=None, form=False, timeout=30):
    """(status, parsed JSON or text). A dict body goes as JSON, or as a form with form=True."""
    headers = dict(headers or {})
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read().decode(errors="replace")
    except urllib.error.URLError as error:
        raise SignInError(f"Nothing answers at {url.split('/')[2]}: {error.reason}") from None
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, raw


def verify_signature(token):
    """Check the token against the key set its issuer publishes, as the clouds do.
    A mismatch almost always means the published keys are out of date: Jenkins
    makes new keys whenever it starts or reloads its configuration."""
    header = json.loads(b64decode(token.split(".")[0]))
    issuer = claims(token)["iss"]
    status, keys = http("GET", issuer.rstrip("/") + "/jwks")
    if status != 200 or not isinstance(keys, dict):
        raise SignInError(f"The issuer {issuer} serves no key set ({status})")
    key = next((k for k in keys.get("keys", []) if k.get("kid") == header.get("kid")), None)
    stale = ("The issuer's published keys are out of date: Jenkins makes new keys when it starts or "
             "reloads its configuration. On the tools host: sudo /opt/release-demo-venv/bin/python "
             "scripts/demos/clouds/oidc_issuer.py publish")
    if key is None:
        raise SignInError(f"The issuer publishes no key {header.get('kid')}. {stale}")
    n, e = (int.from_bytes(b64decode(key[part]), "big") for part in ("n", "e"))
    signed, signature = token.rsplit(".", 1)
    length = (n.bit_length() + 7) // 8
    decrypted = pow(int.from_bytes(b64decode(signature), "big"), e, n).to_bytes(length, "big")
    digest = SHA256_DIGEST_INFO + hashlib.sha256(signed.encode()).digest()
    expected = b"\x00\x01" + b"\xff" * (length - len(digest) - 3) + b"\x00" + digest
    if decrypted != expected:
        raise SignInError(f"The ID token's signature doesn't match the published key {header.get('kid')}. {stale}")


def clock(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%H:%M:%S UTC")


def describe_token(token):
    c = claims(token)
    return (f"ID token from Jenkins for this build: issuer {c['iss']}, subject {c['sub']}, "
            f"build {c.get('build_number', '?')}, expires {clock(c['exp'])}")


class Login:
    """What one cloud gave us: who we are there, until when, and the means to call it."""

    def __init__(self, cloud, who, expires, how, **access):
        self.cloud, self.who, self.expires, self.how, self.access = cloud, who, expires, how, access

    def line(self, cloud_name):
        """One line per cloud for the log: who we are there and when the access ends. Never the token."""
        if self.how != "token":
            return f"  {cloud_name}: signed in as {self.who}"
        left = max(0, round((self.expires - time.time()) / 60))
        return f"  {cloud_name} accepted it: {self.who}, access expires {clock(self.expires)} (in {left} min)"


# AWS ------------------------------------------------------------------------

def aws(settings, token):
    """A one-hour session in the deploy role, from the ID token. Without one, your profile."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import ClientError

    if token is None:
        session = aws_session()
        who = session.client("sts").get_caller_identity()["Arn"].split("/")[-2]
        return Login("aws", f"your profile ({who})", time.time() + 3600, "login", session=session)
    sts = boto3.client("sts", region_name=settings["region"], config=Config(signature_version=UNSIGNED))
    build = claims(token).get("build_number", "local")
    try:
        reply = sts.assume_role_with_web_identity(RoleArn=settings["role_arn"], RoleSessionName=f"jenkins-build-{build}",
                                                  WebIdentityToken=token, DurationSeconds=3600)
    except ClientError as error:
        raise SignInError(f"AWS refused the ID token: {error.response['Error']['Message']}") from None
    creds = reply["Credentials"]
    session = boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                            aws_session_token=creds["SessionToken"], region_name=settings["region"])
    return Login("aws", f"role {settings['role_arn'].split('/')[-1]}", creds["Expiration"].timestamp(), "token",
                 session=session)


# Azure ----------------------------------------------------------------------

def azure(settings, token):
    """An Azure Resource Manager token for the deploy identity, from the ID token.
    Without one, az login's."""
    if token is None:
        reply = _az(["account", "get-access-token", "--resource", "https://management.azure.com/"])
        return Login("azure", "your az login", int(reply["expires_on"]), "login", arm=reply["accessToken"],
                     tenant=reply["tenant"])
    status, reply = http("POST", f"https://login.microsoftonline.com/{settings['tenant_id']}/oauth2/v2.0/token", {
        "client_id": settings["client_id"], "grant_type": "client_credentials", "scope": ARM_SCOPE,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": token}, form=True)
    if status != 200:
        raise SignInError(f"Azure refused the ID token: {_error(reply)}")
    return Login("azure", f"managed identity {settings['identity_name']}", time.time() + int(reply["expires_in"]),
                 "token", arm=reply["access_token"], tenant=settings["tenant_id"])


def acr_password(login, registry):
    """The registry's own refresh token for that Entra token: the password docker
    login uses, with the user 00000000-0000-0000-0000-000000000000."""
    status, reply = http("POST", f"https://{registry}/oauth2/exchange", {
        "grant_type": "access_token", "service": registry, "tenant": login.access["tenant"],
        "access_token": login.access["arm"]}, form=True)
    if status != 200:
        raise SignInError(f"{registry} refused the Azure token: {_error(reply)}")
    return reply["refresh_token"]


def _az(args):
    try:
        result = subprocess.run(["az", *args, "--output", "json"], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        fail("The Azure CLI isn't installed: brew install azure-cli, then az login")
    if result.returncode != 0:
        fail(f"az {' '.join(args[:2])} failed. Sign in first: az login\n{result.stderr.strip()[:300]}")
    return json.loads(result.stdout)


# Google Cloud ---------------------------------------------------------------

def gcp(settings, token):
    """A one-hour token for the deploy service account, from the ID token.
    Without one, gcloud's."""
    if token is None:
        try:
            result = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            fail("The Google Cloud CLI isn't installed: brew install --cask gcloud-cli, then gcloud auth login")
        if result.returncode != 0:
            fail(f"gcloud has no login. Sign in first: gcloud auth login\n{result.stderr.strip()[:300]}")
        return Login("gcp", "your gcloud login", time.time() + 3000, "login", token=result.stdout.strip())
    status, reply = http("POST", "https://sts.googleapis.com/v1/token", {
        "audience": f"//iam.googleapis.com/{settings['provider']}",
        "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
        "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
        "scope": GCP_SCOPE, "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt", "subjectToken": token})
    if status != 200:
        raise SignInError(f"Google Cloud refused the ID token: {_error(reply)}")
    account = settings["service_account"]
    status, access = http("POST", f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{account}:generateAccessToken",
                          {"scope": [GCP_SCOPE], "lifetime": "3600s"},
                          headers={"Authorization": f"Bearer {reply['access_token']}"})
    if status != 200:
        raise SignInError(f"Google Cloud wouldn't act as {account}: {_error(access)}")
    expires = datetime.datetime.strptime(access["expireTime"].split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
    return Login("gcp", f"service account {account.split('@')[0]}",
                 expires.replace(tzinfo=datetime.timezone.utc).timestamp(), "token", token=access["accessToken"])


def _error(reply):
    if isinstance(reply, dict):
        error = reply.get("error")
        if isinstance(error, dict):
            return error.get("message") or json.dumps(error)[:300]
        return reply.get("error_description") or error or json.dumps(reply)[:300]
    return str(reply)[:300]


SIGN_IN = {"aws": aws, "azure": azure, "gcp": gcp}


def sign_in(cloud, settings, log=print):
    """One cloud's access: from the build's ID token in Jenkins, from your own login on the Mac."""
    token = id_token(cloud)
    if token is None and in_jenkins():
        fail(f"No ID token for {cloud}: bind the Jenkins credential oidc-{cloud} as {TOKEN_VARIABLES[cloud]} "
             f"(jenkins_credentials in services.yaml).")
    if token is not None:
        verify_signature(token)
    return SIGN_IN[cloud](settings, token)
