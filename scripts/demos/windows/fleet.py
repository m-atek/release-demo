"""The store fleet on AWS: find the servers, run PowerShell on them through SSM
Run Command, and read the answers. No logins and no inbound ports.

Each step is a PowerShell script in powershell/: fleet.py puts the step's
values on top, then common.ps1, then the step, and sends the whole as one
AWS-RunPowerShellScript command with a comment that says what it does. Values
are written into the script as literals, never as separate parameters, and no
value is a secret: a server reads the JCR token from SSM itself.
"""

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import REGION, SSM_PREFIX, aws_session, fail  # noqa: E402

POWERSHELL = Path(__file__).resolve().parent / "powershell"
DOCUMENT = "AWS-RunPowerShellScript"
TOKEN_PARAMETER = "jcr/store-fleet-token"
DONE = {"Success", "Failed", "TimedOut", "Cancelled", "Cancelling", "Undeliverable", "Terminated",
        "InvalidPlatform", "AccessDenied", "DeliveryTimedOut", "ExecutionTimedOut"}


def deploy_session(service_name):
    """The service's deploy role on the tools host, like orders-service's.
    By hand on the Mac, your own signed-in credentials: the role trusts the
    tools host only."""
    session = aws_session()
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    if ":assumed-role/release-demo-tools-host/" not in identity["Arn"]:
        return session
    role = f"arn:aws:iam::{identity['Account']}:role/release-demo-deploy-{service_name}"
    credentials = sts.assume_role(RoleArn=role, RoleSessionName=f"release-demo-{int(time.time())}")["Credentials"]
    import boto3
    return boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"],
                         aws_session_token=credentials["SessionToken"], region_name=REGION)


def ps_literal(value):
    """A Python value as a PowerShell literal: strings single-quoted with ' doubled."""
    if value is None:
        return "$null"
    if isinstance(value, bool):
        return "$true" if value else "$false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "@{ " + "; ".join(f"{ps_literal(str(k))} = {ps_literal(v)}" for k, v in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        return "@(" + ", ".join(ps_literal(v) for v in value) + ")"
    return "'" + str(value).replace("'", "''") + "'"


def script(step, **values):
    """The full PowerShell for one step: its values, common.ps1, then the step."""
    header = "\n".join(f"${name} = {ps_literal(value)}" for name, value in values.items())
    return "\n".join([header, (POWERSHELL / "common.ps1").read_text(), (POWERSHELL / f"{step}.ps1").read_text()])


def parse_result(output):
    """The JSON on the last RESULT line of a step's output, or None."""
    for line in reversed(output.splitlines()):
        if line.startswith("RESULT "):
            try:
                return json.loads(line[len("RESULT "):])
            except ValueError:
                return None
    return None


@dataclass
class Result:
    name: str
    instance_id: str
    command_id: str
    status: str
    output: str = ""
    error: str = ""
    result: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.status == "Success" and bool(self.result)


class Fleet:
    """The servers of one service's production environment, by their Name tag."""

    def __init__(self, config, session=None):
        self.config = config
        self.env = config["environments"]["production"]
        session = session or deploy_session(config["name"])
        self.ec2 = session.client("ec2")
        self.ssm = session.client("ssm")

    def instances(self, states=("pending", "running", "stopping", "stopped")):
        """{name: {id, state, private_ip}} for the fleet's servers in these states."""
        filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in self.env["tags"].items()]
        filters.append({"Name": "instance-state-name", "Values": list(states)})
        found = {}
        for page in self.ec2.get_paginator("describe_instances").paginate(Filters=filters):
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    name = next((t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"), None)
                    if name in self.env["servers"]:
                        found[name] = {"id": instance["InstanceId"], "state": instance["State"]["Name"],
                                       "private_ip": instance.get("PrivateIpAddress")}
        return dict(sorted(found.items()))

    def running(self, names=None):
        """{name: instance id} for running servers; fails if one of names isn't running."""
        found = {name: i["id"] for name, i in self.instances(("running",)).items()}
        missing = [n for n in (names or self.env["servers"]) if n not in found]
        if names and missing:
            fail(f"Not running: {', '.join(missing)}. Start the fleet: scripts/demo.sh up windows")
        return {name: found[name] for name in (names or found) if name in found}

    def jcr_url(self):
        """JCR on the tools host's private address, which only the fleet may reach on 8082."""
        reply = self.ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": ["release-demo-tools-host"]},
            {"Name": "instance-state-name", "Values": ["running"]}])
        hosts = [i for r in reply["Reservations"] for i in r["Instances"]]
        if not hosts:
            fail("The tools host isn't running, so the fleet can't reach JCR. Start it: scripts/tools.sh up")
        return f"http://{hosts[0]['PrivateIpAddress']}:8082"

    def values(self, **extra):
        """The values every step needs, plus the step's own."""
        return {"InstallDir": self.env["install_dir"], "HealthUrl": self.env["health_url"], "Region": REGION,
                "TokenParameter": SSM_PREFIX + TOKEN_PARAMETER, **extra}

    def table(self, ids, drift=True):
        """Each server's own row for bootstrap and config: its StoreId and UploadEndpoint,
        keyed by instance id. With drift, the drifted server gets no UploadEndpoint."""
        rows = {}
        for name, instance_id in ids.items():
            endpoint = self.env["settings"]["UploadEndpoint"].format(store_id=name)
            if drift and self.env.get("drift", {}).get(name) == "UploadEndpoint":
                endpoint = ""
            rows[instance_id] = {"name": name, "endpoint": endpoint}
        return rows

    def run(self, ids, step, comment, timeout=600, **values):
        """Runs one step on the servers in ids ({name: instance id}) as one command.
        Returns {name: Result} once every server has finished."""
        if not ids:
            return {}
        reply = self.ssm.send_command(
            InstanceIds=list(ids.values()), DocumentName=DOCUMENT, Comment=comment[:100],
            TimeoutSeconds=60, Parameters={"commands": [script(step, **self.values(**values))],
                                           "executionTimeout": [str(timeout)]})
        command_id = reply["Command"]["CommandId"]
        names = {instance_id: name for name, instance_id in ids.items()}
        results = {}
        deadline = time.monotonic() + timeout + 120
        while len(results) < len(ids):
            if time.monotonic() > deadline:
                for instance_id, name in names.items():
                    results.setdefault(name, Result(name, instance_id, command_id, "TimedOut"))
                break
            time.sleep(2)
            for instance_id, name in names.items():
                if name in results:
                    continue
                try:
                    invocation = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
                except self.ssm.exceptions.InvocationDoesNotExist:
                    continue
                if invocation["Status"] in DONE:
                    output = invocation.get("StandardOutputContent", "")
                    results[name] = Result(name, instance_id, command_id, invocation["Status"], output,
                                           invocation.get("StandardErrorContent", ""), parse_result(output) or {})
        return results

    def state(self, ids, comment):
        """What each server runs now: {name: Result} of the state step."""
        return self.run(ids, "state", comment, timeout=120)


def describe_failure(result):
    """One line on why a step failed, from its result or its error output."""
    if result.result.get("error"):
        return result.result["error"]
    if result.result.get("problems"):
        return "; ".join(result.result["problems"])
    if result.result.get("code") is not None and not result.result.get("ok", True):
        return f"/health answered {result.result.get('code') or 'nothing'} with {result.result.get('version')}"
    error = (result.error or "").strip().splitlines()
    return f"Run Command {result.status}" + (f": {error[-1]}" if error else "")
