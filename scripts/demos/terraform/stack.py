"""What demo 2's scripts share: the network stack's paths and roles, running
Terraform, reading a plan, and the run state of a pull request build.

    python scripts/demos/terraform/stack.py credentials plan|apply   an hour of AWS credentials for the container
    python scripts/demos/terraform/stack.py backend                 the state bucket, for terraform init
    python scripts/demos/terraform/stack.py infracost-token         the Infracost CLI token, for the container
    python scripts/demos/terraform/stack.py up                      the roles and the stack's baseline, from the Mac

The pipeline runs Terraform in the toolchain image, and containers can't
reach the tools host's instance role. So the host assumes the plan or the
apply role and hands the container a credentials file in .release/aws/,
valid for an hour, which the pipeline removes when it ends.
"""

import argparse
import configparser
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

# The shared scripts in scripts/, after this folder, so a shared name never hides one of these.
sys.path.append(str(Path(__file__).resolve().parents[2]))
import run_state  # noqa: E402
from common import REGION, ROOT, aws_session, fail, get_parameter, service, ssm_client  # noqa: E402

SERVICE = "network-stack"
DEMO = ROOT / "infra" / "demos" / "2-terraform"
STACK = DEMO / "network"
CHECKS = DEMO / "checks"
WORK = run_state.PATH.parent
AWS_DIR = WORK / "aws"
PROFILES = {"plan": "network-plan", "apply": "network-apply"}
TOOLCHAIN = ROOT / "jenkins" / "toolchain" / "terraform.Dockerfile"


def settings():
    return service(SERVICE)["terraform"]


def relative(path):
    return str(Path(path).resolve().relative_to(ROOT))


# AWS

def session_name():
    """Who assumed the role, as CloudTrail shows it: the Jenkins job and build, or the Mac."""
    name = f"{os.environ.get('JOB_BASE_NAME', 'mac')}-{os.environ.get('BUILD_NUMBER', 'local')}"
    return re.sub(r"[^\w+=,.@-]", "-", name)[:64]


def assume(kind, base=None):
    """Temporary credentials for the plan or apply role, valid for an hour."""
    base = base or aws_session()
    sts = base.client("sts")
    account = sts.get_caller_identity()["Account"]
    role = settings()[f"{kind}_role"]
    reply = sts.assume_role(RoleArn=f"arn:aws:iam::{account}:role/{role}", RoleSessionName=session_name(),
                            DurationSeconds=3600)
    return reply["Credentials"]


def role_session(kind):
    """A boto3 session in the plan or apply role."""
    import boto3

    credentials = assume(kind)
    return boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
                         aws_secret_access_key=credentials["SecretAccessKey"],
                         aws_session_token=credentials["SessionToken"], region_name=REGION)


def credentials_file():
    return AWS_DIR / "credentials"


def write_credentials(kind):
    """The role's credentials as a profile in .release/aws/credentials, readable by this user only."""
    credentials = assume(kind)
    AWS_DIR.mkdir(parents=True, exist_ok=True)
    AWS_DIR.chmod(0o700)
    config = configparser.ConfigParser()
    if credentials_file().exists():
        config.read(credentials_file())
    config[PROFILES[kind]] = {"aws_access_key_id": credentials["AccessKeyId"],
                              "aws_secret_access_key": credentials["SecretAccessKey"],
                              "aws_session_token": credentials["SessionToken"]}
    descriptor = os.open(credentials_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        config.write(handle)
    expires = credentials["Expiration"].astimezone(datetime.timezone.utc).strftime("%H:%M UTC")
    print(f"{settings()[f'{kind}_role']}: credentials until {expires}, profile {PROFILES[kind]}")


def backend_file():
    return WORK / "backend.hcl"


def write_backend(ssm=None):
    """The state bucket for terraform init. Not secret, but the repository never names it."""
    bucket = get_parameter(ssm or ssm_client(), "terraform/state-bucket")
    WORK.mkdir(parents=True, exist_ok=True)
    backend_file().write_text(f'bucket = "{bucket}"\n')


def write_infracost_token(ssm=None):
    """The Infracost CLI token for the container, as a file only this user reads.
    Nothing when it isn't set yet: the pull request then shows the cost as not priced."""
    ssm = ssm or ssm_client()
    try:
        value = ssm.get_parameter(Name="/release-demo/infracost/cli-token", WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        value = "unset"
    if value == "unset":
        print("No Infracost token in SSM yet (infracost/cli-token): the cost stays unpriced.")
        return
    folder = WORK / "secrets"
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    descriptor = os.open(folder / "infracost-token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
    print("Infracost token ready for the container")


# Terraform

def terraform(*args, data_dir, cwd=STACK, profile=None, capture=False, check=True):
    """Terraform in cwd, with its working data in data_dir, so the code's folder stays clean.
    With a profile, it runs as the plan or apply role from .release/aws/credentials."""
    env = {**os.environ, "TF_DATA_DIR": str(data_dir), "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
    if profile:
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            env.pop(key, None)
        env.update(AWS_PROFILE=profile, AWS_SHARED_CREDENTIALS_FILE=str(credentials_file()), AWS_REGION=REGION)
    if not shutil.which("terraform"):
        fail("No terraform here. The pipeline runs it in jenkins/toolchain/terraform.Dockerfile; "
             "on the Mac: brew install hashicorp/tap/terraform")
    result = subprocess.run(["terraform", f"-chdir={cwd}", *args], env=env, text=True,
                            capture_output=capture)
    if check and result.returncode != 0:
        if capture:
            print(scrub(result.stdout + result.stderr), file=sys.stderr)
        fail(f"terraform {args[0]} failed in {relative(cwd)}")
    return result


def init(data_dir, profile, cwd=STACK):
    if not backend_file().exists():
        write_backend()
    terraform("init", "-input=false", "-no-color", "-reconfigure", f"-backend-config={backend_file()}",
              data_dir=data_dir, cwd=cwd, profile=profile, capture=True)


# Plans

ACTIONS = {("create",): "create", ("update",): "update", ("delete",): "destroy",
           ("create", "delete"): "replace", ("delete", "create"): "replace"}
SYMBOLS = {"create": "+", "update": "~", "destroy": "-", "replace": "-/+"}


def plan_summary(plan):
    """What a reviewer reads first: the counts, and each resource with its action."""
    resources = []
    for change in plan.get("resource_changes", []):
        action = ACTIONS.get(tuple(change["change"]["actions"]))
        if action:
            resources.append({"action": action, "address": change["address"]})
    count = lambda *kinds: sum(1 for r in resources if r["action"] in kinds)
    return {"add": count("create", "replace"), "change": count("update"),
            "destroy": count("destroy", "replace"), "resources": resources}


def summary_line(summary):
    return f"{summary['add']} to add, {summary['change']} to change, {summary['destroy']} to destroy"


def has_changes(summary):
    return bool(summary["resources"])


def money(amount, sign=False):
    """$6.60, or +$6.60 and -$1.20 for a difference."""
    amount = Decimal(amount).quantize(Decimal("0.01"))
    prefix = ("+" if amount > 0 else "-" if amount < 0 else "") if sign else ("-" if amount < 0 else "")
    return f"{prefix}${abs(amount):,.2f}"


def scrub(text):
    """Account ids out of anything the public pull request shows."""
    return re.sub(r"\b\d{12}\b", "<account>", text or "")


# The run state of one pull request build: .release/state.json, as for releases.

def load():
    state = run_state.load()
    if not state:
        fail("No run state yet: python scripts/demos/terraform/pr.py init")
    return state


def save(state):
    run_state.save(state)


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


# Setting up, from the Mac

def roots_up():
    """Apply the roles, then the stack as this checkout has it. Terraform shows
    each plan and asks before it changes anything; with nothing to change it
    says so and asks nothing."""
    write_backend()
    print("== The pipeline's roles (infra/demos/2-terraform)")
    data = ROOT / ".local" / "demo-2" / "roles"
    terraform("init", "-input=false", "-reconfigure", f"-backend-config={backend_file()}",
              data_dir=data, cwd=DEMO, capture=True)
    terraform("apply", data_dir=data, cwd=DEMO)
    print("\n== The network stack (infra/demos/2-terraform/network), as the apply role")
    write_credentials("apply")
    data = ROOT / ".local" / "demo-2" / "network"
    init(data, PROFILES["apply"])
    terraform("apply", data_dir=data, profile=PROFILES["apply"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    credentials = sub.add_parser("credentials")
    credentials.add_argument("kind", choices=list(PROFILES))
    sub.add_parser("backend")
    sub.add_parser("infracost-token")
    sub.add_parser("up")
    args = parser.parse_args()

    if args.command == "credentials":
        write_credentials(args.kind)
    elif args.command == "backend":
        write_backend()
    elif args.command == "infracost-token":
        write_infracost_token()
    else:
        roots_up()


if __name__ == "__main__":
    sys.exit(main())
