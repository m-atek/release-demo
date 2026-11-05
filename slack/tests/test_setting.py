"""/setting (video 29): who may change a setting, which keys, and what reaches Jenkins.

    .venv/bin/python -m pytest -q slack/tests/test_setting.py
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("slack_handler_setting", ROOT / "slack" / "handler.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

spec_tests = importlib.util.spec_from_file_location("slack_handler_tests_setting", Path(__file__).resolve().parent / "test_slack_handler.py")
base = importlib.util.module_from_spec(spec_tests)
spec_tests.loader.exec_module(base)

KEY = "free_delivery_until"


def handler_config():
    """What infra/slack_handler.tf writes into config.json, from the real services.yaml and people.yaml."""
    services = yaml.safe_load((ROOT / "services.yaml").read_text())
    people = yaml.safe_load((ROOT / "people.yaml").read_text())
    return {"services": {name: {"jira_project": s["jira"]["project"], "options": s.get("slack", {}).get("options", []),
                                "flags": s.get("flags", {}).get("names", []),
                                "settings": s.get("app_settings", {}).get("keys", [])} for name, s in services.items()},
            "people": {name: p["display_name"] for name, p in people.items() if p.get("releaser")}}


@pytest.fixture
def aws(monkeypatch):
    fakes = SimpleNamespace(ssm=base.FakeSSM(base.SSM_VALUES), lambda_=base.FakeLambda(), ec2=base.FakeEC2("running"))
    monkeypatch.setattr(app, "aws", lambda name: {"ssm": fakes.ssm, "lambda": fakes.lambda_, "ec2": fakes.ec2}[name])
    monkeypatch.setattr(app, "_parameters", {})
    monkeypatch.setattr(app, "config", handler_config)
    monkeypatch.setenv("RELEASERS", "sam")
    monkeypatch.setenv("TOOLS_HOST_ID", "i-0tools")
    monkeypatch.setenv("JENKINS_URL", "https://jenkins-demo.es.nl")
    return fakes


def handed_over(fakes):
    (call,) = fakes.lambda_.invocations
    return json.loads(call["Payload"])["work"]


def test_sams_setting_is_handed_over_with_the_commands_time(aws):
    response = app.handler(base.signed(f"orders-service {KEY} 2026-11-31", command="/setting"), base.CONTEXT)
    assert response["statusCode"] == 200 and response["body"] == ""
    work = handed_over(aws)
    assert work["job"] == "setting"
    assert work["params"] == {"SERVICE": "orders-service", "SETTING": KEY, "VALUE": "2026-11-31",
                              "REQUESTED_BY": base.SAM, "REQUESTED_AT": work["params"]["REQUESTED_AT"]}


def test_the_value_is_the_rest_of_the_line_whatever_it_looks_like(aws):
    """AWS's validator and staging judge a value, not the handler: 31-11-2026 and 30 November must reach them."""
    app.handler(base.signed(f"orders-service {KEY} 30 November", command="/setting"), base.CONTEXT)
    assert handed_over(aws)["params"]["VALUE"] == "30 November"


def test_someone_who_is_not_a_releaser_cant_change_a_setting(aws):
    response = app.handler(base.signed(f"orders-service {KEY} 2026-11-30", command="/setting", user=base.MATEUSZ), base.CONTEXT)
    assert base.ephemeral(response) == app.NOT_A_RELEASER
    assert aws.lambda_.invocations == []


@pytest.mark.parametrize("text, says", [
    ("", "Usage: /setting <service> <setting> <value>"),
    ("orders-service", "Usage: /setting"),
    (f"orders-service {KEY}", "Usage: /setting"),
    ("orders-service price 10", f"orders-service has no setting price. Its settings: {KEY}."),
    (f"pricing-api {KEY} 2026-11-30", "pricing-api has no settings."),
    (f"billing {KEY} 2026-11-30", "There's no service billing."),
    (f"orders-service {KEY} {'x' * 101}", "Keep the value to 100 characters; this one has 101."),
])
def test_wrong_settings_get_a_private_hint(aws, text, says):
    response = app.handler(base.signed(text, command="/setting"), base.CONTEXT)
    assert says in base.ephemeral(response)
    assert aws.lambda_.invocations == []


def test_the_message_names_the_setting_and_who_asked():
    work = {"job": "setting", "params": {"SERVICE": "orders-service", "SETTING": KEY, "VALUE": "x", "REQUESTED_BY": base.SAM}}
    assert app.status_message(work) == (f"*Setting change: {KEY}* · orders-service production · requested by <@{base.SAM}>"
                                        "\n\n:hourglass_flowing_sand:  Starting on Jenkins")


def test_the_manifest_and_terraform_know_setting():
    manifest = yaml.safe_load((ROOT / "slack" / "manifest.yml").read_text())
    commands = {c["command"]: c for c in manifest["features"]["slash_commands"]}
    assert commands["/setting"]["usage_hint"] == "<service> <setting> <value>"
    assert commands["/setting"]["should_escape"] is False
    assert "settings     = try(s.app_settings.keys, [])" in (ROOT / "infra" / "slack_handler.tf").read_text()
