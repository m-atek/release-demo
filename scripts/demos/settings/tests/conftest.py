"""What video 29's tests share: the run state in a temporary folder, a fake AWS AppConfig that runs the real
schema, the real demo state 1.31.0 behind fake function URLs, and a fake ServiceNow. Nothing leaves the machine.

    .venv/bin/python -m pytest -q -p no:cacheprovider scripts/demos/settings/tests
"""

import importlib.util
import io
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

# No test can reach a real account, whatever it ends up calling: no profile, no credentials, no instance role.
for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(name, None)
os.environ.update(AWS_PROFILE="no-such-profile-in-tests", AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                  AWS_EC2_METADATA_DISABLED="true")

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

from botocore.exceptions import ClientError  # noqa: E402

import notify  # noqa: E402
import run_state  # noqa: E402
import setting as steps  # noqa: E402

ROOT = HERE.parents[2]
SCHEMA = json.loads((ROOT / "infra" / "demos" / "29-one-setting" / "settings.schema.json").read_text())
KNOWN = {"application": "app1", "environments": {"staging": "env-s", "production": "env-p"}, "profile": "prof1",
         "quick": "quick1", "bake": "bake1"}
BASELINE = "2026-11-27"
SAM = "U0SAM000001"
URLS = {"staging": "https://staging.example", "production": "https://production.example"}


def schema_problem(content):
    """The real schema, the draft 4 keywords it uses. AWS's words are made up here: the first day reads the real ones."""
    data = json.loads(content)
    for name in SCHEMA["required"]:
        if name not in data:
            return f"object has missing required properties ([\"{name}\"])"
    for name, value in data.items():
        rules = SCHEMA["properties"].get(name)
        if rules is None:
            return f"object instance has properties which are not allowed by the schema: [\"{name}\"]"
        if not isinstance(value, str):
            return f"instance type does not match any allowed primitive type at {name}"
        if not re.search(rules["pattern"], value):
            return f"ECMA 262 regex \"{rules['pattern']}\" does not match input string \"{value}\""
    return None


def bad_request(operation, reason):
    return ClientError({"Error": {"Code": "BadRequestException", "Message": "The input fails to satisfy the constraints"},
                        "Reason": "InvalidConfiguration",
                        "Details": {"InvalidConfiguration": [{"Reason": reason, "Type": "Invalid"}]}}, operation)


class FakeAppConfig:
    """AWS AppConfig as the docs describe it: versions in a store, newest deployment first, one running per
    environment, and the validator at ValidateConfiguration and StartDeployment."""

    def __init__(self, baseline=BASELINE, refuses_at="validate"):
        self.versions = {1: json.dumps({"free_delivery_until": baseline}).encode()}
        self.deployments = {"env-s": [self.done(1, 1)], "env-p": [self.done(1, 1)]}
        self.refuses_at, self.calls = refuses_at, []

    def done(self, number, version, state="COMPLETE"):
        return {"DeploymentNumber": number, "ConfigurationVersion": str(version), "State": state, "EventLog": []}

    def list_hosted_configuration_versions(self, ApplicationId, ConfigurationProfileId):
        return {"Items": [{"VersionNumber": number} for number in sorted(self.versions, reverse=True)]}

    def get_hosted_configuration_version(self, ApplicationId, ConfigurationProfileId, VersionNumber):
        return {"Content": io.BytesIO(self.versions[VersionNumber])}

    def create_hosted_configuration_version(self, **kwargs):
        self.calls.append(("create", kwargs))
        assert kwargs["Description"].isascii() and kwargs["ContentType"] == "application/json"
        if kwargs["LatestVersionNumber"] != max(self.versions):
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "version moved"}}, "CreateHostedConfigurationVersion")
        if self.refuses_at == "create" and schema_problem(kwargs["Content"]):
            raise bad_request("CreateHostedConfigurationVersion", schema_problem(kwargs["Content"]))
        number = max(self.versions) + 1
        self.versions[number] = kwargs["Content"]
        return {"VersionNumber": number}

    def validate_configuration(self, ApplicationId, ConfigurationProfileId, ConfigurationVersion):
        self.calls.append(("validate", ConfigurationVersion))
        problem = schema_problem(self.versions[int(ConfigurationVersion)])
        if problem and self.refuses_at == "validate":
            raise bad_request("ValidateConfiguration", problem)
        return {}

    def start_deployment(self, **kwargs):
        self.calls.append(("start", kwargs))
        if self.refuses_at == "start" and schema_problem(self.versions[int(kwargs["ConfigurationVersion"])]):
            raise bad_request("StartDeployment", schema_problem(self.versions[int(kwargs["ConfigurationVersion"])]))
        going = self.deployments[kwargs["EnvironmentId"]]
        if any(item["State"] in ("BAKING", "DEPLOYING") for item in going):
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "A deployment is in progress"}}, "StartDeployment")
        number = going[0]["DeploymentNumber"] + 1
        state = "BAKING" if kwargs["DeploymentStrategyId"] == "bake1" else "COMPLETE"
        going.insert(0, self.done(number, kwargs["ConfigurationVersion"], state))
        return {"DeploymentNumber": number}

    def list_deployments(self, ApplicationId, EnvironmentId):
        return {"Items": self.deployments[EnvironmentId]}

    def get_deployment(self, ApplicationId, EnvironmentId, DeploymentNumber):
        item = next(d for d in self.deployments[EnvironmentId] if d["DeploymentNumber"] == DeploymentNumber)
        if item["State"] == "BAKING" and not getattr(self, "bakes_forever", False):  # the bake passes between two looks
            item["State"] = getattr(self, "bake_ends", "COMPLETE")
        return dict(item)

    def stop_deployment(self, ApplicationId, EnvironmentId, DeploymentNumber):
        self.calls.append(("stop", DeploymentNumber))
        item = next(d for d in self.deployments[EnvironmentId] if d["DeploymentNumber"] == DeploymentNumber)
        item["State"] = "ROLLED_BACK"

    def served(self, environment):
        """What a function reads through the data API: the version the newest deployment that hasn't been put back carries."""
        for item in self.deployments[KNOWN["environments"][environment]]:
            if item["State"] in ("COMPLETE", "BAKING"):
                return self.versions[int(item["ConfigurationVersion"])]
        return b""


def load_state(version="1.31.0"):
    spec = importlib.util.spec_from_file_location(f"handler_{version.replace('.', '_')}_{id(object())}",
                                                  ROOT / "demo-states" / "orders-service" / version / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Functions:
    """The real demo state behind each fake function URL, reading the fake AWS AppConfig, answering as a function URL does."""

    def __init__(self, appconfig, monkeypatch, version="1.31.0"):
        self.appconfig, self.seen = appconfig, []
        self.module = load_state(version)
        self.values = {}
        monkeypatch.setenv("DEPLOYED_AT", "26 Oct 2026, 09:30 UTC")
        monkeypatch.setattr(self.module, "settings", self.settings, raising=False)

    def settings(self):
        return self.values[os.environ["ENVIRONMENT"]]

    def get(self, url, timeout=5):
        environment = "staging" if url.startswith(URLS["staging"]) else "production"
        path = url.removeprefix(URLS[environment])
        self.seen.append((environment, path))
        os.environ["ENVIRONMENT"] = environment
        self.values[environment] = json.loads(self.appconfig.served(environment) or b"{}")
        event = {"rawPath": path, "requestContext": {"http": {"method": "GET", "path": path}}}
        try:
            reply = self.module.handler(event, None)
        except Exception:
            return 502, "Internal Server Error"
        return reply["statusCode"], reply["body"]


class FakeServiceNow:
    url = "https://servicenow.example"

    def __init__(self, templates=("Setting change of orders-service",)):
        self.templates, self.calls = templates, []

    def find(self, table, query, fields="sys_id"):
        if table == "std_change_record_producer":
            return {"sys_id": "tpl1"} if any(f"name={name}" in query for name in self.templates) else None
        return {"sys_id": f"{table}-1"}

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        if method == "POST":
            return {"sys_id": {"value": "chg1"}, "number": {"value": "CHG0030200"}}
        if method == "GET":
            return {"state": "-5"}
        return {}


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    # No real file, no matter what is called: the ids and Slack are faked for every test.
    monkeypatch.setattr(steps, "ids", lambda service_name: KNOWN)
    monkeypatch.setattr(steps, "appconfig", lambda: pytest.fail("a test asked for the real AWS AppConfig"))
    monkeypatch.setattr(steps, "pause", lambda seconds: None)
    return run_state.PATH


@pytest.fixture
def slack(monkeypatch):
    """The Slack message as it stands after each update."""
    posts = []
    monkeypatch.setattr(notify, "post", lambda state: posts.append(notify.render(state)))
    return posts


@pytest.fixture
def stage(monkeypatch, slack):
    """A run as the pipeline makes one: run_state's fields, then setting.py init, with fake URLs and no log to read."""
    monkeypatch.setattr(steps, "function_url", lambda state, environment: URLS[environment])
    monkeypatch.setattr(steps, "function_error", lambda function, since: None)

    def make(value="31-11-2026", key="free_delivery_until"):
        state = {"service": "orders-service", "version": "1.3.0", "jira_key": "", "jira_keys": [], "commit": "abc",
                 "requested_by": SAM, "slack": {"channel": "C1", "ts": "1.1"},
                 "build": {"number": "7", "url": "https://jenkins.example/job/setting/7/"},
                 "ci": {}, "started_at": 1000.0, "stages": {}}
        steps.init(state, key, value)
        return state

    return make


@pytest.fixture
def aws(monkeypatch):
    fakes = types.SimpleNamespace(appconfig=FakeAppConfig())
    fakes.functions = Functions(fakes.appconfig, monkeypatch)
    monkeypatch.setattr(steps, "http_get", fakes.functions.get)
    return fakes
