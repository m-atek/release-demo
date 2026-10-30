"""The event bridge's AWS AppConfig route (demo 21): what a flag switch, its
completion and its rollback write. AppConfig, CloudWatch, ServiceNow, Slack
and Grafana are fakes.

    .venv/bin/python -m pytest -q bridge/tests
"""

import datetime
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda name: None))
spec = importlib.util.spec_from_file_location("appconfig_route", HERE / "appconfig.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

UTC = datetime.timezone.utc
SAM = "U0SAM000001"
SETTINGS = {"service": "orders-service", "environment": "production", "template": "Feature flag change of orders-service",
            "alarm": "orders-service-production-errors", "watch_minutes": 10,
            "people": {"Sam Developer": {"servicenow_user": "sam.developer"}}}
SECRETS = {"slack/bot-token": "xoxb-test", "slack/channel-id": "C0RELEASES", "servicenow/instance-url": "https://sn.example",
           "servicenow/release-pipeline-password": "pw", "grafana/stack-url": "https://stack.grafana.net",
           "grafana/service-account-token": "glsa_test"}
STARTED = datetime.datetime(2026, 11, 3, 13, 5, 2, tzinfo=UTC)          # 14:05:02 in Amsterdam
ALARM = datetime.datetime(2026, 11, 3, 13, 7, 9, 400000, tzinfo=UTC)    # 14:07:09
ROLLBACK = datetime.datetime(2026, 11, 3, 13, 7, 40, 900000, tzinfo=UTC)  # 14:07:40


class FakeAppConfig:
    def __init__(self, deployment, tags=None, enabled=True):
        self.deployment, self.tags, self.enabled = deployment, dict(tags or {}), enabled

    def get_deployment(self, ApplicationId, EnvironmentId, DeploymentNumber):
        return dict(self.deployment)

    def get_hosted_configuration_version(self, ApplicationId, ConfigurationProfileId, VersionNumber):
        content = {"version": "1", "values": {"delivery_estimate_v2": {"enabled": self.enabled}}}
        return {"Content": io.BytesIO(json.dumps(content).encode())}

    def list_tags_for_resource(self, ResourceArn):
        return {"Tags": dict(self.tags)}

    def tag_resource(self, ResourceArn, Tags):
        self.tags.update(Tags)


class FakeCloudWatch:
    def describe_alarm_history(self, **kwargs):
        def item(at, state):
            return {"Timestamp": at, "HistoryData": json.dumps({"newState": {"stateValue": state}})}

        return {"AlarmHistoryItems": [item(ROLLBACK + datetime.timedelta(minutes=3), "OK"), item(ALARM, "ALARM"),
                                      item(STARTED - datetime.timedelta(hours=2), "ALARM")]}


class FakeServiceNow:
    def __init__(self):
        self.opened, self.closed, self.count = [], [], 30120

    def open(self, settings, fields, requested_by, started, minutes=10):
        self.count += 1
        self.opened.append((fields, requested_by, started, minutes))
        return f"id-{self.count}", f"CHG00{self.count}"

    def close(self, change_id, code, notes, ended):
        self.closed.append((change_id, code, notes, ended))


@pytest.fixture
def world(monkeypatch):
    def setup(deployment, tags=None, enabled=True):
        fakes = types.SimpleNamespace(appconfig=FakeAppConfig(deployment, tags, enabled), cloudwatch=FakeCloudWatch(),
                                      sn=FakeServiceNow(), slack=[], annotations=[])
        monkeypatch.setattr(bridge, "aws", lambda name: {"appconfig": fakes.appconfig, "cloudwatch": fakes.cloudwatch}[name])
        monkeypatch.setattr(bridge, "config", lambda: SETTINGS)
        monkeypatch.setattr(bridge, "parameters", lambda: SECRETS)
        monkeypatch.setattr(bridge, "ServiceNow", lambda url, password: fakes.sn)

        def slack(token, method, **fields):
            fakes.slack.append((method, fields))
            return {"ok": True, "channel": "C0RELEASES", "ts": "1793.0001"}

        monkeypatch.setattr(bridge, "slack", slack)
        monkeypatch.setattr(bridge, "annotate", lambda secrets, text, at: fakes.annotations.append((text, at)))
        return fakes

    return setup


def deployment(**extra):
    return {"DeploymentNumber": 7, "ConfigurationVersion": "4", "ConfigurationProfileId": "prof1",
            "DeploymentStrategyId": "strat1", "Description": "Sam Developer in Slack", "StartedAt": STARTED,
            "FinalBakeTimeInMinutes": 10, "EventLog": [], **extra}


def event(kind, environment="production"):
    return {"source": "aws.appconfig", "detail-type": "On Deployment Start", "detail": {
        "Type": kind, "Application": {"Id": "app1", "Name": "orders-service"},
        "Environment": {"Id": "env1", "Name": environment}, "ConfigurationProfile": {"Id": "prof1", "Name": "flags"},
        "DeploymentNumber": 7, "ConfigurationVersion": "4"}}


CONTEXT = types.SimpleNamespace(invoked_function_arn="arn:aws:lambda:eu-west-1:123456789012:function:release-demo-appconfig-events")
TAGS = {"requested_by": SAM, "flag": "delivery_estimate_v2", "state": "on"}


def test_a_switch_on_opens_a_pre_approved_change_and_posts_its_message(world):
    fakes = world(deployment(), TAGS)
    assert bridge.handler(event("OnDeploymentStart"), CONTEXT)["recorded"]
    (fields, requested_by, started, minutes), = fakes.sn.opened
    assert fields["short_description"] == "Flag delivery_estimate_v2 on in orders-service production"
    assert requested_by == "sam.developer" and started == STARTED and minutes == 10
    assert fields["correlation_id"] == "appconfig:app1/environment/env1/deployment/7"
    (method, posted), = fakes.slack
    assert method == "chat.postMessage" and posted["channel"] == "C0RELEASES"
    assert posted["text"] == ("*Flag change: delivery_estimate_v2 on* · orders-service production · requested by <@U0SAM000001>"
                              "\n\nCHG0030121 · AWS AppConfig deployment 7 · watching orders-service-production-errors "
                              "for 10 minutes")
    assert fakes.annotations == [("flag delivery_estimate_v2 on (Sam Developer)", STARTED)]
    assert fakes.appconfig.tags["change_number"] == "CHG0030121" and fakes.appconfig.tags["slack_ts"] == "1793.0001"


def test_a_retried_start_records_nothing_twice(world):
    fakes = world(deployment(), TAGS)
    bridge.handler(event("OnDeploymentStart"), CONTEXT)
    bridge.handler(event("OnDeploymentStart"), CONTEXT)
    assert len(fakes.sn.opened) == 1 and len(fakes.slack) == 1


def test_a_switch_that_bakes_without_an_alarm_closes_successful(world):
    fakes = world(deployment(CompletedAt=STARTED + datetime.timedelta(minutes=10, seconds=3)), TAGS)
    bridge.handler(event("OnDeploymentStart"), CONTEXT)
    bridge.handler(event("OnDeploymentComplete"), CONTEXT)
    (_, code, notes, _), = fakes.sn.closed
    assert code == "successful"
    assert notes == ("AWS AppConfig deployment 7 completed at 14:15:05: Done: delivery_estimate_v2 is on in production. "
                     "No alarm in 10 minutes.")
    method, updated = fakes.slack[-1]
    assert method == "chat.update" and updated["ts"] == "1793.0001"
    assert updated["text"].endswith("\n\nDone: delivery_estimate_v2 is on in production. No alarm in 10 minutes.")
    assert fakes.appconfig.tags["recorded"] == "complete"


def test_a_rollback_on_the_alarm_closes_the_switch_and_records_the_switch_back(world):
    log = [{"EventType": "ROLLBACK_COMPLETED", "TriggeredBy": "APPCONFIG", "OccurredAt": ROLLBACK + datetime.timedelta(seconds=2)},
           {"EventType": "ROLLBACK_STARTED", "TriggeredBy": "CLOUDWATCH_ALARM", "OccurredAt": ROLLBACK},
           {"EventType": "DEPLOYMENT_STARTED", "TriggeredBy": "USER", "OccurredAt": STARTED}]
    fakes = world(deployment(EventLog=log), TAGS)
    bridge.handler(event("OnDeploymentStart"), CONTEXT)
    bridge.handler(event("OnDeploymentRolledBack"), CONTEXT)
    first, second = fakes.sn.closed
    assert first[1] == "unsuccessful"
    assert first[2] == ("AWS AppConfig rolled it back at 14:07:40, 31s after alarm orders-service-production-errors "
                        "went into ALARM at 14:07:09.")
    fields, requested_by, started, _ = fakes.sn.opened[1]
    assert fields["short_description"] == ("Flag delivery_estimate_v2 off in orders-service production, by AWS AppConfig "
                                           "on alarm")
    assert requested_by == "release.pipeline" and started == ROLLBACK
    assert second[1] == "successful" and second[3] == ROLLBACK + datetime.timedelta(seconds=2)
    _, updated = fakes.slack[-1]
    assert updated["text"].endswith("\n\nRolled back by alarm orders-service-production-errors at 14:07:40, 31s after it "
                                    "fired. delivery_estimate_v2 is off again. CHG0030122")
    assert fakes.annotations[-1][0] == "flag delivery_estimate_v2 off: rolled back by alarm"
    bridge.handler(event("OnDeploymentRolledBack"), CONTEXT)  # a retry
    assert len(fakes.sn.opened) == 2


def test_resets_switch_is_recorded_too_requested_by_the_pipeline(world):
    fakes = world(deployment(Description="reset.py before the next take", FinalBakeTimeInMinutes=0,
                             CompletedAt=STARTED + datetime.timedelta(seconds=4)),
                  {"requested_by": "reset.py", "flag": "delivery_estimate_v2", "state": "off"}, enabled=False)
    bridge.handler(event("OnDeploymentStart"), CONTEXT)
    bridge.handler(event("OnDeploymentComplete"), CONTEXT)
    (fields, requested_by, _, _), = fakes.sn.opened
    assert requested_by == "release.pipeline"
    assert fields["short_description"] == "Flag delivery_estimate_v2 off in orders-service production"
    (_, posted), (_, updated) = fakes.slack
    assert posted["text"] == ("*Flag change: delivery_estimate_v2 off* · orders-service production · requested by reset.py"
                              "\n\nCHG0030121 · AWS AppConfig deployment 7")
    assert updated["text"].endswith("Done: delivery_estimate_v2 is off in production.")


def test_staging_isnt_recorded(world):
    fakes = world(deployment(), TAGS)
    assert bridge.handler(event("OnDeploymentStart", environment="staging"), CONTEXT) == {"recorded": False}
    assert fakes.sn.opened == [] and fakes.slack == []


def test_amsterdam_time_without_zone_data(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_zoneinfo(name, *args, **kwargs):
        if name == "zoneinfo":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_zoneinfo)
    assert bridge.clock(datetime.datetime(2026, 11, 3, 13, 5, 2, tzinfo=UTC)) == "14:05:02"       # CET
    assert bridge.clock(datetime.datetime(2026, 7, 3, 13, 5, 2, tzinfo=UTC)) == "15:05:02"        # CEST
    assert bridge.clock(datetime.datetime(2026, 10, 25, 0, 59, tzinfo=UTC)) == "02:59:00"         # just before the change
    assert bridge.clock(datetime.datetime(2026, 10, 25, 1, 0, tzinfo=UTC)) == "02:00:00"
