"""Helpers the stage scripts share: paths, services.yaml, git, HTTP and SSM."""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SSM_PREFIX = "/release-demo/"
REGION = "eu-west-1"


def services():
    return yaml.safe_load((ROOT / "services.yaml").read_text())


def service(name):
    all_services = services()
    if name not in all_services:
        fail(f"Unknown service {name}. services.yaml has: {', '.join(all_services)}")
    config = all_services[name]
    config["name"] = name
    config["dir"] = ROOT / config["path"]
    return config


def version_of(directory):
    return (Path(directory) / "VERSION").read_text().strip()


def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def http_get(url, timeout=5):
    """GET a URL. Returns (status, body); status 0 when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, ""


def aws_session():
    """Your release-demo profile on the Mac; the instance role on the tools host."""
    import boto3

    profile = os.environ.get("AWS_PROFILE")
    if not profile and "release-demo" in boto3.Session().available_profiles:
        profile = "release-demo"
    return boto3.Session(profile_name=profile, region_name=REGION)


def ssm_client():
    """SSM, checked with one call, or a clear message to sign in."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client = aws_session().client("ssm")
        client.describe_parameters(MaxResults=1)
        return client
    except (BotoCoreError, ClientError) as error:
        fail(f"Can't reach SSM ({error.__class__.__name__}). On the Mac, sign in: aws sso login --profile release-demo")


def put_parameter(client, name, value, secure=True):
    client.put_parameter(Name=SSM_PREFIX + name, Value=value, Overwrite=True,
                         Type="SecureString" if secure else "String")


def get_parameter(client, name):
    value = client.get_parameter(Name=SSM_PREFIX + name, WithDecryption=True)["Parameter"]["Value"]
    if value == "unset":
        fail(f"{SSM_PREFIX}{name} isn't set yet. See: python scripts/secrets.py")
    return value


def github_api(method, path, token, body=None, scheme="Bearer"):
    request = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "Authorization": f"{scheme} {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def github_app_token(client):
    """An installation token for the GitHub App on release-demo, valid for an hour.

    The app's JWT is signed with openssl. The private key reaches it through a
    pipe, so it never touches the disk.
    """
    import base64
    import time

    app_id = get_parameter(client, "github/app-id")
    key = get_parameter(client, "github/app-private-key")
    repository = get_parameter(client, "github/repository")

    encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")
    now = int(time.time())
    signing_input = encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + encode(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    read_end, write_end = os.pipe()
    os.write(write_end, key.encode())
    os.close(write_end)
    try:
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_end}"],
                                   input=signing_input, capture_output=True, pass_fds=(read_end,), check=True).stdout
    finally:
        os.close(read_end)
    jwt = (signing_input + b"." + encode(signature)).decode()

    installation = github_api("GET", f"/repos/{repository}/installation", jwt)
    token = github_api("POST", f"/app/installations/{installation['id']}/access_tokens", jwt)
    return token["token"], repository, installation


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)
