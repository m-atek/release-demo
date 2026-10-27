"""Make the read-only JFrog Container Registry token the inventory servers use.

    python scripts/demos/linux/jcr_token.py           from the Mac: runs itself on the tools host
    python scripts/demos/linux/jcr_token.py --here    on the tools host

JCR answers only on the tools host. There, with the admin password from the
host's tmpfs, this makes a token for the transient user inventory-reader in
the group readers: it can read packages, and JCR refuses its writes. It is
stored in SSM as jcr/inventory-read-token through the deploy role, because
the host's own role can't write it. The cloud servers read it with their
instance role; Jenkins hands it to dc-agent as jcr-inventory-read-token. A
stored token that still reads is kept. Prints no token.
"""

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

JCR = "http://localhost:8082/artifactory"
PARAMETER = "/release-demo/jcr/inventory-read-token"
SECRETS = Path("/run/release-demo/secrets")
YEAR = 365 * 24 * 3600


def call(method, path, auth, data=None):
    request = urllib.request.Request(f"{JCR}/{path}", data=data, method=method, headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def here():
    import boto3

    session = boto3.Session(region_name="eu-west-1")
    account = session.client("sts").get_caller_identity()["Account"]
    creds = session.client("sts").assume_role(RoleArn=f"arn:aws:iam::{account}:role/release-demo-deploy-inventory",
                                              RoleSessionName=f"inventory-{int(time.time())}")["Credentials"]
    ssm = boto3.client("ssm", region_name="eu-west-1", aws_access_key_id=creds["AccessKeyId"],
                       aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
    current = session.client("ssm").get_parameter(Name=PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    if current != "unset" and call("GET", "api/repositories", f"Bearer {current}")[0] == 200:
        print("inventory-reader: the stored token still reads")
        return
    password = (SECRETS / "jcr-admin-password").read_text().strip()
    admin = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    form = urllib.parse.urlencode({"username": "inventory-reader", "scope": "applied-permissions/groups:readers",
                                   "expires_in": str(YEAR)}).encode()
    status, body = call("POST", "api/security/token", admin, form)
    token = json.loads(body).get("access_token") if status == 200 else None
    if not token:
        sys.exit(f"JCR didn't make the token ({status})")
    write = call("PUT", "demos-candidates/inventory/read-only-check.txt", f"Bearer {token}", b"check")[0]
    if write in (200, 201):
        sys.exit("The new token can write to JCR: not storing it. Check the readers group's permissions.")
    ssm.put_parameter(Name=PARAMETER, Value=token, Type="SecureString", Overwrite=True)
    print(f"inventory-reader: made a read-only token for a year (a write answers {write}) and stored it in SSM")


def from_mac():
    """Send this file to the tools host through SSM Run Command and run it there."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common import aws_session

    session = aws_session()
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": ["release-demo-tools-host"]}, {"Name": "instance-state-name", "Values": ["running"]}])
    instances = [i["InstanceId"] for r in reply["Reservations"] for i in r["Instances"]]
    if not instances:
        sys.exit("The tools host isn't running: scripts/tools.sh up")
    source = Path(__file__).read_text()
    command = f"/opt/release-demo-venv/bin/python - --here <<'PYTHON'\n{source}\nPYTHON"
    ssm = session.client("ssm")
    sent = ssm.send_command(InstanceIds=instances, DocumentName="AWS-RunShellScript",
                            Parameters={"commands": [command]})["Command"]["CommandId"]
    for _ in range(60):
        time.sleep(2)
        try:
            result = ssm.get_command_invocation(CommandId=sent, InstanceId=instances[0])
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            print((result["StandardOutputContent"] + result["StandardErrorContent"]).strip())
            sys.exit(0 if result["Status"] == "Success" else 1)
    sys.exit("The tools host didn't answer within 2 minutes")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--here", action="store_true", help="on the tools host")
    args = parser.parse_args()
    here() if args.here else from_mac()


if __name__ == "__main__":
    main()
