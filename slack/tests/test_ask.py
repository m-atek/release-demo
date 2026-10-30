"""/ask (demo 23): anyone in people.yaml with Slack may ask; the question goes to Jenkins as a parameter."""

import json

from test_slack_handler import CONTEXT, MATEUSZ, RESPONSE_URL, SAM, app, aws, ephemeral, handed_over, signed  # noqa: F401


def test_a_team_member_who_isnt_a_releaser_may_ask(aws, monkeypatch):
    monkeypatch.setenv("ASKERS", "mateusz,sam")
    aws.ssm.values["slack/member-id/mateusz"] = MATEUSZ
    response = app.handler(signed("How do I roll back orders-service?", command="/ask", user=MATEUSZ), CONTEXT)
    assert response == {"statusCode": 200, "body": ""}
    work = handed_over(aws)
    assert work == {"job": "runbook-assistant-ask", "channel": "C0RELEASES", "response_url": RESPONSE_URL,
                    "params": {"QUESTION": "How do I roll back orders-service?", "ASKED_BY": MATEUSZ}}


def test_someone_outside_people_yaml_may_not(aws, monkeypatch):
    monkeypatch.setenv("ASKERS", "sam")
    text = ephemeral(app.handler(signed("How do I roll back?", command="/ask", user="U0STRANGER1"), CONTEXT))
    assert text == app.NOT_IN_THE_TEAM and aws.lambda_.invocations == []


def test_a_question_is_3_to_300_characters(aws, monkeypatch):
    monkeypatch.setenv("ASKERS", "sam")
    assert ephemeral(app.handler(signed("hi", command="/ask"), CONTEXT)) == app.ASK_USAGE
    assert ephemeral(app.handler(signed("x" * 301, command="/ask"), CONTEXT)) == app.ASK_USAGE
    assert aws.lambda_.invocations == []


def test_the_status_message_quotes_the_question():
    work = {"job": "runbook-assistant-ask", "params": {"QUESTION": "How do I roll back orders-service?",
                                                       "ASKED_BY": SAM}}
    assert app.status_message(work).startswith(f"Question from <@{SAM}>: How do I roll back orders-service?")


def test_the_manifest_has_ask():
    import yaml
    from pathlib import Path

    manifest = yaml.safe_load((Path(app.__file__).parent / "manifest.yml").read_text())
    commands = {c["command"]: c for c in manifest["features"]["slash_commands"]}
    assert commands["/ask"]["description"] == "Ask the runbook assistant"
    assert commands["/ask"]["usage_hint"] == "<question>"
    assert json.dumps(sorted(commands)) == json.dumps(["/ask", "/release", "/rollback"])
