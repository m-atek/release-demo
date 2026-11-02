"""/sbom (video 24): anyone in people.yaml with Slack may search, a stranger is refused, a bad id gets
the usage line, and the job it starts is never release or rollback. Every call is a fake.

    .venv/bin/python -m pytest -q slack/tests
"""

import urllib.parse
from pathlib import Path

import pytest
import yaml

from test_slack_handler import (CONTEXT, MATEUSZ, RESPONSE_URL, SAM, app, aws, calls, ephemeral, handed_over,  # noqa: F401
                                signed)

STRANGER = "U0STRANGER1"
READING = ":hourglass_flowing_sand:  Reading the SBOMs of what's live"


@pytest.fixture
def team(aws, monkeypatch):  # noqa: F811
    """Sam releases; Mateusz is in people.yaml with Slack and doesn't, as in the real file."""
    monkeypatch.setenv("ASKERS", "mateusz,sam")
    monkeypatch.setenv("RELEASERS", "sam")
    aws.ssm.values["slack/member-id/mateusz"] = MATEUSZ
    return aws


@pytest.mark.parametrize("user", [SAM, MATEUSZ])
def test_sam_and_mateusz_may_search(team, user):
    event = signed("CVE-2026-111497", command="/sbom", user=user)
    response = app.handler(event, CONTEXT)
    assert response == {"statusCode": 200, "body": ""}
    assert handed_over(team) == {"job": "sbom-search", "channel": "C0RELEASES", "response_url": RESPONSE_URL,
                                 "params": {"VULN": "CVE-2026-111497", "ASKED_BY": user,
                                            "REQUESTED_AT": event["headers"]["X-Slack-Request-Timestamp"]}}


def test_a_stranger_is_refused_in_private(team):
    assert ephemeral(app.handler(signed("CVE-2026-111497", command="/sbom", user=STRANGER), CONTEXT)) == "You can't search here."
    assert team.lambda_.invocations == []


def test_an_unset_member_id_searches_nothing(team):
    team.ssm.values["slack/member-id/mateusz"] = "unset"
    assert ephemeral(app.handler(signed("CVE-2026-111497", command="/sbom", user=MATEUSZ), CONTEXT)) == app.NOT_A_SEARCHER


@pytest.mark.parametrize("text", ["", "hello", "CVE-2026", "CVE-26-111497", "CVE-2026-111497 CVE-2026-111498", "CVE-2026-111497; rm -rf /",
                                  "$(whoami)", "GHSA-9v8h-qqgm", "GHSA-9v8h-qqgm-4m35x", "--vuln CVE-2026-111497", "CVE-2026-111497\nCVE-1"])
def test_a_bad_id_gets_the_usage_line_and_starts_nothing(team, text):
    says = ephemeral(app.handler(signed(text, command="/sbom"), CONTEXT))
    assert says == "Usage: /sbom <CVE or GHSA id>, for example /sbom CVE-2026-111497"
    assert team.lambda_.invocations == []


@pytest.mark.parametrize("text, vulnerability", [("cve-2026-111497", "CVE-2026-111497"), ("  CVE-2018-20060 ", "CVE-2018-20060"),
                                                 ("GHSA-9v8h-qqgm-4m35", "GHSA-9v8h-qqgm-4m35"),
                                                 ("ghsa-9V8H-QQGM-4M35", "GHSA-9v8h-qqgm-4m35")])
def test_an_id_reaches_jenkins_as_grype_and_github_write_it(team, text, vulnerability):
    app.handler(signed(text, command="/sbom"), CONTEXT)
    assert handed_over(team)["params"]["VULN"] == vulnerability


def test_the_handler_and_the_search_hold_the_same_two_patterns():
    root = Path(app.__file__).resolve().parents[1]
    for source in ((root / "slack" / "handler.py").read_text(), (root / "scripts" / "demos" / "patch_clock" / "search.py").read_text()):
        assert 'r"CVE-[0-9]{4}-[0-9]+"' in source and 'r"ghsa-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}"' in source


def test_sbom_can_never_start_a_release_or_a_rollback(team):
    """Whatever follows /sbom, for anyone, the only job it hands over is sbom-search."""
    for text in ("orders-service ORD-12", "orders-service ORD-12 standard", "orders-service", "CVE-2026-111497"):
        for user in (SAM, MATEUSZ):
            team.lambda_.invocations.clear()
            app.handler(signed(text, command="/sbom", user=user), CONTEXT)
            jobs = {handed_over(team)["job"]} if team.lambda_.invocations else set()
            assert jobs <= {"sbom-search"}


def test_mateusz_who_may_search_still_cant_release_or_roll_back(team):
    for command, text in (("/release", "orders-service ORD-12"), ("/rollback", "orders-service")):
        assert ephemeral(app.handler(signed(text, command=command, user=MATEUSZ), CONTEXT)) == app.NOT_A_RELEASER
    assert team.lambda_.invocations == []


def sbom_work():
    return {"job": "sbom-search", "channel": "C0RELEASES", "response_url": RESPONSE_URL,
            "params": {"VULN": "CVE-2026-111497", "ASKED_BY": SAM, "REQUESTED_AT": "1793000000"}}


def test_the_message_comes_first_then_the_job_with_its_message_and_time(team, calls):  # noqa: F811
    app.handler({"work": sbom_work()}, CONTEXT)
    (message, _), = calls.slack_calls("chat.postMessage")
    assert message["text"] == f"*Search for CVE-2026-111497* · asked by <@{SAM}>\n\n{READING}"
    request, = calls.jenkins
    assert request.full_url == "https://jenkins-demo.es.nl/job/sbom-search/buildWithParameters?delay=0sec"
    assert {k: v[0] for k, v in urllib.parse.parse_qs(request.data.decode()).items()} == {
        "VULN": "CVE-2026-111497", "ASKED_BY": SAM, "REQUESTED_AT": "1793000000", "SLACK_CHANNEL": "C0RELEASES",
        "SLACK_TS": "1793000000.000100"}


def test_with_the_tools_host_stopped_slack_says_so_in_one_line(team, calls):  # noqa: F811
    team.ec2.state = "stopped"
    app.handler({"work": sbom_work()}, CONTEXT)
    assert calls.replies() == [app.STOPPED] and calls.jenkins == [] and calls.slack_calls("chat.postMessage") == []


def test_jenkins_refusing_turns_the_message_into_the_reason(team, calls):  # noqa: F811
    calls.jenkins_answers = [503]
    app.handler({"work": sbom_work()}, CONTEXT)
    (update, _), = calls.slack_calls("chat.update")
    assert update["text"].startswith(f"*Search for CVE-2026-111497* · asked by <@{SAM}>\n\n:x:  Not started: Jenkins doesn't answer")


def test_the_manifest_has_sbom():
    manifest = yaml.safe_load((Path(app.__file__).parent / "manifest.yml").read_text())
    command = next(c for c in manifest["features"]["slash_commands"] if c["command"] == "/sbom")
    assert command["description"] == "Search the SBOMs of what's live"
    assert command["usage_hint"] == "<CVE or GHSA id>"
    assert command["url"] == "https://REPLACE-WITH-SLACK-HANDLER-URL.invalid/"
