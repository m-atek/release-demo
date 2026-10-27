"""What demo 4's scripts share: pricing-api's settings, its deploy role, the
alias and its weights, the APIs' addresses, and the lines a stage adds to the
release's Slack message and ServiceNow change.

The scripts here run from the repository root like the shared ones, which
they import from scripts/. Their own deploy.py and check.py shadow the shared
ones by name, so shared() loads a shared script under another name.
"""

import importlib.util
import os
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

import run_state  # noqa: E402
from common import ROOT, aws_session, get_parameter, service, ssm_client  # noqa: E402

SERVICE = "pricing-api"
ALARM_PREFIX = f"{SERVICE}-canary-"


def config():
    return service(SERVICE)


def environment(name):
    return config()["environments"][name]


def shared(name):
    """A shared script from scripts/ as a module, such as shared("deploy")."""
    module_name = f"shared_{name}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


@lru_cache(maxsize=1)
def deploy_session():
    """A session as release-demo-deploy-pricing-api, which may change only
    pricing-api's functions, their aliases and the canary's own alarms."""
    import boto3

    session = aws_session()
    account = session.client("sts").get_caller_identity()["Account"]
    creds = session.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/release-demo-deploy-{SERVICE}",
        RoleSessionName=f"{SERVICE}-{int(time.time())}")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=session.region_name)


def lambda_client():
    return deploy_session().client("lambda")


def cloudwatch_client():
    return deploy_session().client("cloudwatch")


def api_url(environment_name):
    """The API Gateway address Terraform wrote to SSM, without a trailing slash."""
    return get_parameter(ssm_client(), environment(environment_name)["api_url_parameter"]).rstrip("/")


def routing(client, function):
    """The alias live: the version it points at, and any extra versions with their weights."""
    alias = client.get_alias(FunctionName=function, Name="live")
    weights = alias.get("RoutingConfig", {}).get("AdditionalVersionWeights", {})
    return alias["FunctionVersion"], {number: float(weight) for number, weight in weights.items()}


def route(client, function, main, extra=None, weight=0.0):
    """Point live at main, and send weight (0 to 1) of its traffic to extra."""
    weights = {extra: round(weight, 4)} if extra and weight > 0 else {}
    client.update_alias(FunctionName=function, Name="live", FunctionVersion=main,
                        RoutingConfig={"AdditionalVersionWeights": weights})


_app_versions = {}


def app_version(client, function, number):
    """The app version a Lambda version runs, from its description or its package."""
    key = (function, number)
    if key not in _app_versions:
        _app_versions[key] = shared("deploy").app_version(client, function, number)
    return _app_versions[key]


def canary_alarms(client=None):
    """Names of the canary alarms that exist now: there should be none between runs."""
    client = client or cloudwatch_client()
    names = []
    for page in client.get_paginator("describe_alarms").paginate(AlarmNamePrefix=ALARM_PREFIX):
        names += [alarm["AlarmName"] for alarm in page["MetricAlarms"]]
    return names


def in_release():
    """True inside a pipeline run with a Slack message to add to."""
    return bool(run_state.load().get("slack", {}).get("ts"))


def tell(text):
    """One line for the release's Slack message and a work note on its change.
    Outside a release (a run from the Mac without Slack) it only prints."""
    print(text, flush=True)
    state = run_state.load()
    python = sys.executable or "python3"
    if state.get("slack", {}).get("ts"):
        subprocess.run([python, str(SCRIPTS / "notify.py"), "line", text], cwd=ROOT, check=False)
    if state.get("change"):
        subprocess.run([python, str(SCRIPTS / "change.py"), "note", text], cwd=ROOT, check=False)


def workspace():
    """The Jenkins workspace, or the repository when run by hand: where dist/ and .release/ live."""
    return Path(os.environ.get("WORKSPACE", ROOT))
