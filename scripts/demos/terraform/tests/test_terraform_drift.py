import datetime
from types import SimpleNamespace

import pytest

import drift
import stack

HTTPS = {"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr_blocks": ["10.42.0.0/16"],
         "description": "HTTPS from inside the VPC", "ipv6_cidr_blocks": [], "prefix_list_ids": [],
         "security_groups": [], "self": False}
SSH = {**HTTPS, "from_port": 22, "to_port": 22, "cidr_blocks": ["0.0.0.0/0"], "description": ""}


def test_an_inbound_rule_added_by_hand_reads_as_one_line():
    plan = {"resource_drift": [{"address": "aws_security_group.app", "change": {
        "actions": ["update"], "before": {"ingress": [HTTPS], "tags_all": {}}, "after": {"ingress": [HTTPS, SSH], "tags_all": {"x": 1}}}}]}
    assert drift.describe(plan) == ["aws_security_group.app: inbound rule added: tcp 22 from 0.0.0.0/0"]


def test_rules_tags_and_deletions():
    plan = {"resource_drift": [
        {"address": "aws_security_group.app", "change": {"actions": ["update"],
                                                         "before": {"egress": [HTTPS]}, "after": {"egress": []}}},
        {"address": "aws_vpc.main", "change": {"actions": ["update"], "before": {"tags": {"Name": "a"}},
                                               "after": {"tags": {"Name": "b"}}}},
        {"address": "aws_subnet.private[\"b\"]", "change": {"actions": ["delete"], "before": {}, "after": None}},
    ]}
    assert drift.describe(plan) == [
        "aws_security_group.app: outbound rule removed: tcp 443 to 10.42.0.0/16 (\"HTTPS from inside the VPC\")",
        "aws_vpc.main: tag Name changed from a to b",
        "aws_subnet.private[\"b\"]: deleted outside Terraform",
    ]


def test_all_traffic():
    rule = {"protocol": "-1", "from_port": 0, "to_port": 0, "cidr_blocks": [], "self": True}
    assert drift.rule_text(rule, "from") == "all traffic from itself"


# A run that a change in AWS started (drift_trigger.py)

APP_ID = "sg-0a1b2c3d4e5f60718"
BUCKET = "release-demo-store-exports-20261029abcdef"
MADE = datetime.datetime(2026, 10, 29, 16, 54, 24, tzinfo=datetime.timezone.utc).timestamp()
FROM_THE_CONSOLE = {"DRIFT_TRIGGER": "event", "EVENT_NAME": "AuthorizeSecurityGroupIngress",
                    "EVENT_TIME": "2026-10-29T16:54:24Z", "RESOURCE_ID": APP_ID, "FROM_CONSOLE": "true"}
RULE = "aws_security_group.app: inbound rule added: tcp 22 from 0.0.0.0/0"
SUMMARY = "0 to add, 1 to change, 0 to destroy"
LINK, INCIDENT = "<https://jenkins-demo.es.nl/job/network-drift/9/|Drift check #9>", "<https://sn/inc|INC0010009>"
FOUND = "Found 74 s after a console change: AuthorizeSecurityGroupIngress on network-stack-app."


def stack_plan():
    """A plan with the stack's state, as terraform show -json writes it, with only what drift.py reads."""
    return {"prior_state": {"values": {"root_module": {"resources": [
        {"address": "aws_security_group.app",
         "values": {"id": APP_ID, "name": "network-stack-app", "tags": {"Name": "network-stack-app"}}},
        {"address": "aws_s3_bucket.exports", "values": {"id": BUCKET, "bucket": BUCKET, "tags": {}}},
    ]}}}, "resource_drift": []}


def test_a_run_a_console_change_started_says_how_long_after_it_the_drift_was_found():
    change = drift.change_that_started(FROM_THE_CONSOLE)
    assert change == {"name": "AuthorizeSecurityGroupIngress", "time": MADE, "resource": APP_ID, "console": True}
    assert drift.found_line(change, stack_plan(), MADE + 74.4) == FOUND


def test_a_change_through_the_api_on_a_bucket_reads_with_the_buckets_name():
    change = drift.change_that_started({**FROM_THE_CONSOLE, "EVENT_NAME": "PutBucketPolicy", "RESOURCE_ID": BUCKET,
                                        "FROM_CONSOLE": "false"})
    assert drift.found_line(change, stack_plan(), MADE + 151) == (
        f"Found 151 s after a change through the API: PutBucketPolicy on {BUCKET}.")


def test_a_resource_the_plan_does_not_know_keeps_its_id():
    change = drift.change_that_started({**FROM_THE_CONSOLE, "RESOURCE_ID": "sg-0123456789abcdef0"})
    assert drift.found_line(change, {}, MADE + 90).endswith("AuthorizeSecurityGroupIngress on sg-0123456789abcdef0.")


def test_the_name_comes_from_the_drift_when_the_state_has_no_match():
    plan = {"resource_drift": [{"address": "aws_default_security_group.main", "change": {
        "actions": ["update"], "before": {"id": "sg-0f1e2d3c4b5a69788", "name": "default",
                                          "tags": {"Name": "network-stack-default-unused"}}, "after": {}}}]}
    assert drift.resource_name(plan, "sg-0f1e2d3c4b5a69788") == "network-stack-default-unused"


@pytest.mark.parametrize("environ", [
    {},
    {"DRIFT_TRIGGER": "timer"},
    {"DRIFT_TRIGGER": "manual"},
    {**FROM_THE_CONSOLE, "DRIFT_TRIGGER": "manual"},
    {**FROM_THE_CONSOLE, "EVENT_NAME": ""},
    {**FROM_THE_CONSOLE, "EVENT_TIME": "soon"},
])
def test_the_0700_run_and_runs_by_hand_have_no_change(environ):
    assert drift.change_that_started(environ) is None


def test_the_0700_run_and_runs_by_hand_say_what_they_said_before():
    head = (":warning: *Drift in network-stack*: AWS no longer matches main.\n"
            f"• {RULE}\nTo match main again, Terraform would: {SUMMARY}.\n")
    assert drift.drift_message([RULE], SUMMARY, LINK, INCIDENT, by_timer=True) == (
        head + f"Found by the 07:00 check on main. {LINK} · {INCIDENT}")
    assert drift.drift_message([RULE], SUMMARY, LINK, INCIDENT, by_timer=False) == (
        head + "Found by the drift check on main (it runs weekdays at 07:00; this run was started by hand). "
        f"{LINK} · {INCIDENT}")
    assert drift.incident_text([RULE], SUMMARY, "https://b/9/").startswith(
        "The morning drift check found the network stack in AWS different from main.\n\nChanged outside Terraform:\n")


def test_a_run_a_change_started_adds_its_line_and_keeps_the_rest():
    assert drift.drift_message([RULE], SUMMARY, LINK, INCIDENT, by_timer=False, found=FOUND) == (
        ":warning: *Drift in network-stack*: AWS no longer matches main.\n"
        f"• {RULE}\nTo match main again, Terraform would: {SUMMARY}.\n{FOUND}\n{LINK} · {INCIDENT}")
    text = drift.incident_text([RULE], SUMMARY, "https://b/9/", FOUND)
    assert text.startswith("The drift check found the network stack in AWS different from main.\n"
                           f"{FOUND}\n\nChanged outside Terraform:\n- {RULE}\n\n")
    assert "morning" not in text


def report_with(monkeypatch, work, environ, now):
    """drift.py report after a plan that found the hand-made rule, with Slack,
    ServiceNow, SSM and the events faked. Returns (Slack text, incident's found line)."""
    plan = stack_plan()
    plan["resource_drift"] = [{"address": "aws_security_group.app", "change": {
        "actions": ["update"], "before": {"ingress": [HTTPS]}, "after": {"ingress": [HTTPS, SSH]}}}]
    plan["resource_changes"] = [{"address": "aws_security_group.app", "change": {"actions": ["update"]}}]
    monkeypatch.setattr(drift, "RESULT", work / "drift.json")
    monkeypatch.setattr(drift, "PLAN_JSON", work / "drift-plan.json")
    stack.write_json(drift.RESULT, {"exit_code": 2})
    stack.write_json(drift.PLAN_JSON, plan)
    for name in ("DRIFT_TRIGGER", "EVENT_NAME", "EVENT_TIME", "RESOURCE_ID", "FROM_CONSOLE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"BUILD_NUMBER": "9", "BUILD_URL": "https://jenkins-demo.es.nl/job/network-drift/9/",
                        "GIT_COMMIT": "a5671af0", **environ}.items():
        monkeypatch.setenv(name, value)
    posted, opened = [], []
    monkeypatch.setattr(drift, "ssm_client", lambda: None)
    monkeypatch.setattr(drift, "get_parameter", lambda ssm, name: f"test-{name}")
    monkeypatch.setattr(drift.notify, "slack", lambda method, token, **fields: posted.append(fields["text"]))
    monkeypatch.setattr(drift, "send", lambda *args, **kwargs: None)
    monkeypatch.setattr(drift, "open_incident", lambda lines, summary, url, found=None: (
        opened.append(found) or ("INC0010009", "https://sn/inc", True)))
    monkeypatch.setattr(drift, "time", SimpleNamespace(time=lambda: now))
    drift.report(None)
    (text,), (found,) = posted, opened
    return text, found


def test_the_incident_and_slack_give_the_same_measured_time(monkeypatch, work):
    text, found = report_with(monkeypatch, work, FROM_THE_CONSOLE, MADE + 74)
    assert found == FOUND
    assert text.endswith(f"\n{FOUND}\n{LINK} · {INCIDENT}")
    assert "inbound rule added: tcp 22 from 0.0.0.0/0" in text


def test_the_0700_report_is_unchanged(monkeypatch, work):
    text, found = report_with(monkeypatch, work, {"DRIFT_TRIGGER": "timer"}, MADE + 74)
    assert found is None
    assert text.endswith(f"\nFound by the 07:00 check on main. {LINK} · {INCIDENT}")
    assert "Found 74 s" not in text
