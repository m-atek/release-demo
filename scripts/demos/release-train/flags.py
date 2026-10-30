"""orders-service's feature flags in AWS AppConfig, for demo 21's train and reset.

    import flags
    flags.production_values()                 {flag: True|False}, as production's functions read them
    flags.switch("staging", "delivery_estimate_v2", False, "reset.py before the next take")

The ids come from SSM (appconfig/orders-service), which
infra/demos/21-release-train-flags writes. The tools host's role may read
and deploy this application's flags, nothing else (that root's iam.tf).
"""

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))

import yaml  # noqa: E402

from common import aws_session, get_parameter, service, ssm_client  # noqa: E402

SERVICE = "orders-service"
RUNNING = ("BAKING", "DEPLOYING", "VALIDATING", "ROLLING_BACK")


def ids():
    return json.loads(get_parameter(ssm_client(), f"appconfig/{SERVICE}"))


def client(name="appconfig"):
    return aws_session().client(name)


def declared(directory=None):
    """flags.yaml of the version in the service folder: {flag: {issue, default, description}}."""
    path = Path(directory or service(SERVICE)["dir"]) / "flags.yaml"
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


def values_of(content):
    """{flag: True|False} from a feature flags profile's version, or from the data API's answer."""
    data = json.loads(content) if isinstance(content, (bytes, str)) else content
    values = data.get("values", data) if isinstance(data, dict) else {}
    return {name: bool((value or {}).get("enabled")) for name, value in values.items() if isinstance(value, dict)}


def production_values(environment="production"):
    """What the functions read: the data API's answer for the environment. {} when nothing is deployed."""
    settings = service(SERVICE)["environments"][environment]["settings"]
    data = client("appconfigdata")
    try:
        token = data.start_configuration_session(
            ApplicationIdentifier=settings["APPCONFIG_APPLICATION"], EnvironmentIdentifier=settings["APPCONFIG_ENVIRONMENT"],
            ConfigurationProfileIdentifier=settings["APPCONFIG_PROFILE"])["InitialConfigurationToken"]
        content = data.get_latest_configuration(ConfigurationToken=token)["Configuration"].read()
    except data.exceptions.ResourceNotFoundException:
        return {}
    except data.exceptions.BadRequestException:
        return {}
    return values_of(content) if content else {}


def latest_version(appconfig, known):
    versions = appconfig.list_hosted_configuration_versions(ApplicationId=known["application"],
                                                            ConfigurationProfileId=known["profile"])["Items"]
    return max(v["VersionNumber"] for v in versions)


def content_of(appconfig, known, number):
    return json.loads(appconfig.get_hosted_configuration_version(
        ApplicationId=known["application"], ConfigurationProfileId=known["profile"], VersionNumber=number)["Content"].read())


def deployments(appconfig, known, environment):
    return appconfig.list_deployments(ApplicationId=known["application"],
                                      EnvironmentId=known["environments"][environment])["Items"]


def current_values(appconfig, known, environment):
    """The flags the environment's last completed deployment put there; {} before any."""
    for item in deployments(appconfig, known, environment):
        if item["State"] == "COMPLETE":
            return values_of(content_of(appconfig, known, int(item["ConfigurationVersion"])))
    return {}


def running(appconfig, known, environment):
    """The deployment still going in the environment, if any: its number."""
    for item in deployments(appconfig, known, environment):
        if item["State"] in RUNNING:
            return item["DeploymentNumber"]
    return None


def stop(appconfig, known, environment, number, wait=120):
    appconfig.stop_deployment(ApplicationId=known["application"], EnvironmentId=known["environments"][environment],
                              DeploymentNumber=number)
    deadline = time.monotonic() + wait
    while running(appconfig, known, environment) and time.monotonic() < deadline:
        time.sleep(3)


def switch(environment, name, on, description, requested_by="reset.py", strategy="reset_strategy", wait=120):
    """A new version with one flag changed, deployed to the environment; waits until it completes."""
    known = ids()
    appconfig = client()
    latest = latest_version(appconfig, known)
    content = content_of(appconfig, known, latest)
    content.setdefault("values", {})[name] = {**content["values"].get(name, {}), "enabled": on}
    made = appconfig.create_hosted_configuration_version(
        ApplicationId=known["application"], ConfigurationProfileId=known["profile"], Content=json.dumps(content).encode(),
        ContentType="application/json", LatestVersionNumber=latest, Description=f"{name} {'on' if on else 'off'}, {description}")
    started = appconfig.start_deployment(
        ApplicationId=known["application"], EnvironmentId=known["environments"][environment],
        DeploymentStrategyId=known[strategy], ConfigurationProfileId=known["profile"],
        ConfigurationVersion=str(made["VersionNumber"]), Description=description,
        Tags={"requested_by": requested_by, "flag": name, "state": "on" if on else "off"})
    deadline = time.monotonic() + wait
    while running(appconfig, known, environment) and time.monotonic() < deadline:
        time.sleep(3)
    return started["DeploymentNumber"]
