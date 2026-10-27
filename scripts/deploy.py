"""Stages 6 and 10, Staging and Production: deploy the exact stored package.

    python scripts/deploy.py orders-service staging
    python scripts/deploy.py orders-service production --back         back to the version before this run
    python scripts/deploy.py orders-service production --previous     back to the previous release (/rollback)

Downloads the package from JFrog Container Registry, checks its SHA-256
against the one recorded at build, updates the function, publishes a Lambda
version whose description is the app version, and moves the alias live.
Runs on the tools host with the service's deploy role, which can change only
that service's functions.
"""

import argparse
import hashlib
import io
import re
import sys
import time
import urllib.request
import zipfile

import run_state
from common import aws_session, fail, get_parameter, service, ssm_client
from publish import download


def lambda_client(service_name):
    session = aws_session()
    account = session.client("sts").get_caller_identity()["Account"]
    role = f"arn:aws:iam::{account}:role/release-demo-deploy-{service_name}"
    creds = session.client("sts").assume_role(RoleArn=role, RoleSessionName=f"release-demo-{int(time.time())}")["Credentials"]
    return session.client("lambda", aws_access_key_id=creds["AccessKeyId"],
                          aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])


def alias_version(client, function):
    return client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]


def app_version(client, function, number):
    """The app version a Lambda version runs: its description, which this script
    sets, or for the version Terraform published first, the VERSION in its package."""
    description = client.get_function_configuration(FunctionName=function, Qualifier=number)["Description"]
    if re.fullmatch(r"\d+\.\d+\.\d+", description):
        return description
    location = client.get_function(FunctionName=function, Qualifier=number)["Code"]["Location"]
    with urllib.request.urlopen(location, timeout=30) as response:
        return zipfile.ZipFile(io.BytesIO(response.read())).read("VERSION").decode().strip()


def move_alias(client, function, number):
    client.update_alias(FunctionName=function, Name="live", FunctionVersion=number)


def previous_release(client, function):
    """The newest published version before live whose app version differs from live's."""
    current = alias_version(client, function)
    current_app = app_version(client, function, current)
    versions = []
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        versions += [v for v in page["Versions"] if v["Version"] != "$LATEST"]
    for v in sorted(versions, key=lambda v: int(v["Version"]), reverse=True):
        if int(v["Version"]) < int(current):
            app = app_version(client, function, v["Version"])
            if app != current_app:
                return v["Version"], app
    fail(f"{function} has no earlier release to roll back to.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    back = parser.add_mutually_exclusive_group()
    back.add_argument("--back", action="store_true", help="move live back to where this run found it")
    back.add_argument("--previous", action="store_true", help="move live to the previous release")
    args = parser.parse_args()

    config = service(args.service)
    function = config["environments"][args.environment]["function"]
    client = lambda_client(args.service)
    state = run_state.load()
    deploys = state.setdefault("deploys", {})

    if args.back or args.previous:
        if args.back:
            number = deploys[args.environment]["previous_number"]
            app = deploys[args.environment]["previous_version"]
        else:
            number, app = previous_release(client, function)
            deploys[args.environment] = {"previous_number": alias_version(client, function),
                                         "previous_version": app_version(client, function, alias_version(client, function))}
        move_alias(client, function, number)
        deploys[args.environment].update({"rolled_back_to": app, "rolled_back_number": number,
                                          "url": client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"]})
        run_state.save(state)
        print(f"{function}: live is back on version {number} ({app})")
        return

    token = get_parameter(ssm_client(), "jcr/token")
    package = download(token, state["package"]["path"])
    actual = hashlib.sha256(package).hexdigest()
    if actual != state["package"]["sha256"]:
        fail(f"{state['package']['path']} has SHA-256 {actual}, but the build recorded {state['package']['sha256']}. "
             f"Not deploying a package that changed after it was published.")
    print(f"SHA-256 verified: {actual}")

    previous_number = alias_version(client, function)
    previous_app = app_version(client, function, previous_number)
    client.update_function_code(FunctionName=function, ZipFile=package)
    client.get_waiter("function_updated_v2").wait(FunctionName=function)
    published = client.publish_version(FunctionName=function, Description=state["version"])
    move_alias(client, function, published["Version"])
    url = client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"]
    deploys[args.environment] = {"number": published["Version"], "previous_number": previous_number,
                                 "previous_version": previous_app, "url": url, "deployed_at": run_state.now()}
    run_state.save(state)
    print(f"{function}: version {published['Version']} ({state['version']}) is live, was {previous_number} ({previous_app})")


if __name__ == "__main__":
    sys.exit(main())
