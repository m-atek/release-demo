"""/flag (demo 21): who may switch a flag, which flags, and what reaches AWS AppConfig.

    .venv/bin/python -m pytest -q slack/tests/test_flag.py
"""

import importlib.util
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("slack_handler_flag", ROOT / "slack" / "handler.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

sys_path_test = Path(__file__).resolve().parent
spec_tests = importlib.util.spec_from_file_location("slack_handler_tests", sys_path_test / "test_slack_handler.py")
base = importlib.util.module_from_spec(spec_tests)
spec_tests.loader.exec_module(base)

IDS = {"application": "app1", "environments": {"staging": "env-s", "production": "env-p"}, "profile": "prof1",
       "strategy": "strat1"}


def handler_config():
    """What infra/slack_handler.tf writes into config.json, from the real services.yaml and people.yaml."""
    services = yaml.safe_load((ROOT / "services.yaml").read_text())
    people = yaml.safe_load((ROOT / "people.yaml").read_text())
    return {"services": {name: {"jira_project": s["jira"]["project"], "options": s.get("slack", {}).get("options", []),
                                "flags": s.get("flags", {}).get("names", [])} for name, s in services.items()},
            "people": {name: p["display_name"] for name, p in people.items() if p.get("releaser")}}


class FakeAppConfig:
    def __init__(self, busy=False):
        self.busy, self.created, self.started = busy, [], []
        self.content = {"version": "1", "flags": {"delivery_estimate_v2": {"name": "delivery_estimate_v2"}},
                        "values": {"delivery_estimate_v2": {"enabled": False}}}

    def list_hosted_configuration_versions(self, ApplicationId, ConfigurationProfileId):
        return {"Items": [{"VersionNumber": 1}, {"VersionNumber": 3}, {"VersionNumber": 2}]}

    def get_hosted_configuration_version(self, ApplicationId, ConfigurationProfileId, VersionNumber):
        assert VersionNumber == 3
        return {"Content": io.BytesIO(json.dumps(self.content).encode())}

    def create_hosted_configuration_version(self, **kwargs):
        self.created.append(kwargs)
        return {"VersionNumber": 4}

    def start_deployment(self, **kwargs):
        if self.busy:
            raise type("ConflictException", (Exception,), {})("Deployment in progress")
        self.started.append(kwargs)
        return {"DeploymentNumber": 9}


@pytest.fixture
def aws(monkeypatch):
    values = {**base.SSM_VALUES, "appconfig/orders-service": json.dumps(IDS)}
    fakes = SimpleNamespace(ssm=base.FakeSSM(values), lambda_=base.FakeLambda(), ec2=base.FakeEC2("stopped"),
                            appconfig=FakeAppConfig())
    monkeypatch.setattr(app, "aws", lambda name: {"ssm": fakes.ssm, "lambda": fakes.lambda_, "ec2": fakes.ec2,
                                                  "appconfig": fakes.appconfig}[name])
    monkeypatch.setattr(app, "_parameters", {})
    monkeypatch.setattr(app, "config", handler_config)
    monkeypatch.setenv("RELEASERS", "sam")
    monkeypatch.setenv("TOOLS_HOST_ID", "i-0tools")
    monkeypatch.setenv("JENKINS_URL", "https://jenkins-demo.es.nl")
    said = []
    monkeypatch.setattr(app, "say", lambda url, text: said.append(text))
    fakes.said = said
    return fakes


def handed_over(fakes):
    (call,) = fakes.lambda_.invocations
    return json.loads(call["Payload"])["work"]


def test_sam_switching_a_flag_is_answered_at_once_and_handed_over(aws):
    response = app.handler(base.signed("orders-service delivery_estimate_v2 on", command="/flag"), base.CONTEXT)
    assert base.ephemeral(response) == "Switching delivery_estimate_v2 on in orders-service production."
    work = handed_over(aws)
    assert work["job"] == "flag"
    assert work["params"] == {"SERVICE": "orders-service", "FLAG": "delivery_estimate_v2", "STATE": "on",
                              "PERSON": "Sam Developer", "REQUESTED_BY": base.SAM,
                              "REQUESTED_AT": work["params"]["REQUESTED_AT"]}


def test_someone_who_is_not_a_releaser_cant_switch_a_flag(aws):
    response = app.handler(base.signed("orders-service delivery_estimate_v2 on", command="/flag", user=base.MATEUSZ),
                           base.CONTEXT)
    assert base.ephemeral(response) == app.NOT_A_RELEASER
    assert aws.lambda_.invocations == []


@pytest.mark.parametrize("text, says", [
    ("orders-service", "Usage: /flag <service> <flag> on|off"),
    ("orders-service delivery_estimate_v2 maybe", "Usage: /flag <service> <flag> on|off"),
    ("orders-service dark_mode on", "orders-service has no flag dark_mode. Its flags: delivery_estimate_v2."),
    ("pricing-api delivery_estimate_v2 on", "pricing-api has no feature flags."),
    ("billing delivery_estimate_v2 on", "There's no service billing."),
    ("orders-service delivery_estimate_v2 on staging", "/flag switches flags in production only"),
])
def test_wrong_flags_get_a_private_hint(aws, text, says):
    response = app.handler(base.signed(text, command="/flag"), base.CONTEXT)
    assert says in base.ephemeral(response)
    assert aws.lambda_.invocations == []


def test_the_switch_writes_one_new_version_and_deploys_it_to_production(aws):
    app.run({"job": "flag", "response_url": base.RESPONSE_URL, "channel": "C0RELEASES",
             "params": {"SERVICE": "orders-service", "FLAG": "delivery_estimate_v2", "STATE": "on",
                        "PERSON": "Sam Developer", "REQUESTED_BY": base.SAM}})
    (created,) = aws.appconfig.created
    assert json.loads(created["Content"])["values"] == {"delivery_estimate_v2": {"enabled": True}}
    assert created["LatestVersionNumber"] == 3 and created["ConfigurationProfileId"] == "prof1"
    (started,) = aws.appconfig.started
    assert started == {"ApplicationId": "app1", "EnvironmentId": "env-p", "DeploymentStrategyId": "strat1",
                       "ConfigurationProfileId": "prof1", "ConfigurationVersion": "4",
                       "Description": "Sam Developer in Slack",
                       "Tags": {"requested_by": base.SAM, "flag": "delivery_estimate_v2", "state": "on"}}
    assert aws.said == []  # nothing to say: the event bridge posts the message


def test_a_switch_while_a_deployment_bakes_says_so(aws):
    aws.appconfig.busy = True
    app.run({"job": "flag", "response_url": base.RESPONSE_URL, "channel": "C0RELEASES",
             "params": {"SERVICE": "orders-service", "FLAG": "delivery_estimate_v2", "STATE": "on",
                        "PERSON": "Sam Developer", "REQUESTED_BY": base.SAM}})
    assert aws.said == ["delivery_estimate_v2 can't switch now: an AWS AppConfig deployment still runs in "
                        "orders-service production. It ends when its 10 minutes of bake time are over."]


def test_the_manifest_and_terraform_know_flag():
    manifest = yaml.safe_load((ROOT / "slack" / "manifest.yml").read_text())
    commands = {c["command"]: c for c in manifest["features"]["slash_commands"]}
    assert commands["/flag"]["usage_hint"] == "<service> <flag> on|off"
    terraform = (ROOT / "infra" / "slack_handler.tf").read_text()
    assert "flags        = try(s.flags.names, [])" in terraform
    assert re.search(r"people\s+=\s+\{ for name in local.releasers", terraform)
