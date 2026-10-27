"""Keep the store fleet's read-only JCR token in SSM. Idempotent.

    python scripts/demos/windows/jcr_token.py           from the Mac: runs on the tools host
    python scripts/demos/windows/jcr_token.py --here    on the tools host itself

The fleet downloads store-sync from JCR with a token of its own, so it can be
revoked on its own and can only read: a token for the user store-fleet, which
JCR doesn't need to know, scoped to the readers group (read on every
repository, no deploy, no delete). JCR's free edition makes such tokens
through its token API with the admin password, on the tools host. The token is
stored as /release-demo/jcr/store-fleet-token, through the store-sync deploy
role, which may write that one parameter. A stored token that still works is
kept. Prints what it did, never the token.

--here needs only boto3, so it runs on a host that has an older checkout.
"""

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PARAMETER = "/release-demo/jcr/store-fleet-token"
USER = "store-fleet"
SCOPE = "applied-permissions/groups:readers"
DAYS = 90
JCR = "http://localhost:8082"


def jcr(method, path, auth, form=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    headers = {"Authorization": auth}
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(JCR + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def works(token):
    """A readers-scoped token that JCR still accepts, valid for at least a week."""
    import time
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return False
    if SCOPE not in claims.get("scp", "") or claims.get("exp", 0) < time.time() + 7 * 24 * 3600:
        return False
    return jcr("GET", "/artifactory/api/repositories", f"Bearer {token}")[0] == 200


def here():
    import boto3
    session = boto3.Session(region_name="eu-west-1")
    host_ssm = session.client("ssm")
    account = session.client("sts").get_caller_identity()["Account"]
    credentials = session.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/release-demo-deploy-store-sync",
        RoleSessionName="store-fleet-token")["Credentials"]
    deploy_ssm = boto3.client("ssm", region_name="eu-west-1", aws_access_key_id=credentials["AccessKeyId"],
                              aws_secret_access_key=credentials["SecretAccessKey"],
                              aws_session_token=credentials["SessionToken"])

    current = deploy_ssm.get_parameter(Name=PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    if current != "unset" and works(current):
        print(f"{PARAMETER}: the stored token still works, kept")
        return

    password = host_ssm.get_parameter(Name="/release-demo/jcr/admin-password", WithDecryption=True)["Parameter"]["Value"]
    admin = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    status, body = jcr("POST", "/artifactory/api/security/token", admin,
                       {"username": USER, "scope": SCOPE, "expires_in": str(DAYS * 24 * 3600)})
    if status != 200:
        print(f"JCR didn't create the token ({status}): {body[:200]}", file=sys.stderr)
        sys.exit(1)
    token = json.loads(body)["access_token"]
    if not works(token):
        print("JCR made a token that doesn't work. Check the readers group in JCR.", file=sys.stderr)
        sys.exit(1)
    deploy_ssm.put_parameter(Name=PARAMETER, Value=token, Type="SecureString", Overwrite=True)
    print(f"{PARAMETER}: created a read-only token for {USER}, valid for {DAYS} days")


def main():
    if "--here" in sys.argv:
        return here()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from host import run_python
    code, out, err = run_python(Path(__file__).read_text(), "store-sync: keep the fleet's read-only JCR token",
                                args=["--here"])
    print(out.strip())
    if code != 0:
        print(err.strip()[-1000:], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
