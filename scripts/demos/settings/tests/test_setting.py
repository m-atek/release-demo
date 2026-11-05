"""The job's steps, against the real schema and the real demo state 1.31.0: what AWS refuses, what staging stops,
what goes through, and what each leaves behind."""

import json
import re

import pytest
from botocore.exceptions import ClientError

import change as changes
import notify
import run_state
import setting as steps
from conftest import BASELINE, KNOWN, SAM, SCHEMA, FakeAppConfig, FakeServiceNow, schema_problem

KEY = "free_delivery_until"


def body(aws):
    return json.loads(aws.appconfig.versions[max(aws.appconfig.versions)])


def is_refused(state, aws):
    return steps.validate(state, aws.appconfig, KNOWN) == steps.REFUSED


def fake_finish(state, result):
    """notify.py finish, without the process around it."""
    total = run_state.duration(5)
    state["total"] = total
    text = {"live": f"Live in {total}.",
            "failed": (state["finish_line"].format(total=total) if state.get("finish_line") else
                       f"Stopped after {total}, after the production deploy. Look at production now."
                       if "production" in state.get("deploys", {}) else f"Stopped after {total}. Nothing reached production.")}[result]
    state.setdefault("lines", []).append(text)
    notify.post(state)


# The schema.

@pytest.mark.parametrize("value, passes", [
    ("2026-11-30", True), ("2026-11-31", True), ("2026-02-30", True), ("31-11-2026", False), ("30 November", False),
    ("2026-13-01", False), ("2026-11-32", False), ("2026-11-3", False), ("", False),
])
def test_the_schema_checks_the_shape_of_a_date_and_not_the_calendar(value, passes):
    assert (schema_problem(json.dumps({KEY: value})) is None) is passes


def test_the_schema_is_draft_4_and_allows_only_the_keys_it_names():
    assert SCHEMA["$schema"] == "http://json-schema.org/draft-04/schema#" and SCHEMA["additionalProperties"] is False
    assert schema_problem(json.dumps({KEY: "2026-11-30", "other": "x"}))
    assert schema_problem(json.dumps({}))


# Try 1: AWS refuses the file.

def test_first_try_aws_refuses_in_its_own_words_and_nothing_deploys(stage, aws, slack):
    state = stage("31-11-2026")
    assert is_refused(state, aws)
    message = slack[-1]
    assert "AWS AppConfig refused version 2 of the settings file:" in message
    assert 'does not match input string "31-11-2026"' in message
    assert [call for call in aws.appconfig.calls if call[0] == "start"] == []
    assert state["setting"]["before"] == BASELINE and f"From {BASELINE} to 31-11-2026" in message


@pytest.mark.parametrize("where", ["create", "validate", "start"])
def test_the_refusal_reads_the_same_whichever_call_says_no(stage, aws, slack, where):
    """Docs tie the details to CreateHostedConfigurationVersion, the validator to StartDeployment and ValidateConfiguration: the first day shows which."""
    aws.appconfig.refuses_at = where
    state = stage("31-11-2026")
    code = steps.validate(state, aws.appconfig, KNOWN)
    if where == "start":
        assert code == 0
        code = steps.deploy(state, "staging", aws.appconfig, KNOWN)
    assert code == steps.REFUSED
    assert 'does not match input string "31-11-2026"' in slack[-1] and "refused" in slack[-1]


def test_a_refusal_without_details_gives_aws_message():
    error = ClientError({"Error": {"Code": "BadRequestException", "Message": "Configuration failed validation"}}, "StartDeployment")
    assert steps.aws_words(error) == ["Configuration failed validation"]


def test_no_arn_or_account_id_reaches_slack():
    error = ClientError({"Error": {"Code": "BadRequestException",
                                   "Message": "No access to arn:aws:appconfig:eu-west-1:123456789012:application/abc in 123456789012"}},
                        "StartDeployment")
    assert steps.aws_words(error) == ["No access to ... in ..."]


def test_aws_words_are_escaped_for_slack(stage, aws, slack):
    def refusal(*args, **kwargs):
        raise ClientError({"Error": {"Code": "BadRequestException", "Message": "a <b> & c"}}, "ValidateConfiguration")

    aws.appconfig.validate_configuration = refusal
    state = stage("2026-11-30")
    assert is_refused(state, aws)
    assert "> a &lt;b&gt; &amp; c" in slack[-1]


def test_a_value_is_escaped_in_the_head(stage, aws, slack):
    state = stage("<b>&")
    assert "To &lt;b&gt;&amp;" in state["head"]


def test_the_whole_answer_goes_in_the_build_log(stage, aws, slack, capsys):
    state = stage("31-11-2026")
    is_refused(state, aws)
    assert "AWS AppConfig's whole answer:" in capsys.readouterr().out


def test_another_error_is_not_a_refusal(stage, aws, slack):
    def broken(*args, **kwargs):
        raise ClientError({"Error": {"Code": "InternalServerException", "Message": "boom"}}, "ValidateConfiguration")

    aws.appconfig.validate_configuration = broken
    with pytest.raises(ClientError):
        steps.validate(stage("2026-11-30"), aws.appconfig, KNOWN)


def test_a_refused_version_doesnt_ride_along_with_the_next_change(stage, aws, slack):
    is_refused(stage("31-11-2026"), aws)
    state = stage("2026-11-30")
    assert steps.validate(state, aws.appconfig, KNOWN) == 0
    assert state["setting"]["before"] == BASELINE
    assert body(aws) == {KEY: "2026-11-30"}


def test_the_value_already_there_changes_nothing(stage, aws, slack):
    state = stage(BASELINE)
    assert is_refused(state, aws)
    assert f"{KEY} is {BASELINE} in production already. Nothing to change." in slack[-1]
    assert not [call for call in aws.appconfig.calls if call[0] == "create"]


def test_a_version_that_doesnt_read_settings_changes_nothing(stage, aws, slack, monkeypatch):
    monkeypatch.setattr(steps, "http_get", lambda url, timeout=5: (200, '{"version": "1.3.0"}'))
    state = stage("2026-11-30")
    assert is_refused(state, aws)
    assert "orders-service 1.3.0 doesn't read settings in staging. Nothing changed." in slack[-1]
    assert aws.appconfig.calls == []


def test_a_function_that_doesnt_answer_changes_nothing(stage, aws, slack, monkeypatch):
    monkeypatch.setattr(steps, "http_get", lambda url, timeout=5: (0, ""))
    state = stage("2026-11-30")
    assert is_refused(state, aws)
    assert "orders-service doesn't answer on /health in staging. Nothing changed." in slack[-1]


def test_an_unknown_key_stops_at_init(stage):
    with pytest.raises(SystemExit):
        stage("2026-11-30", key="price_of_everything")


# Try 2: the file is fine, and staging stops it.

def through_validator(state, aws):
    assert steps.validate(state, aws.appconfig, KNOWN) == 0


def test_second_try_passes_aws_then_staging_fails_on_a_real_error_and_goes_back(stage, aws, slack, monkeypatch):
    monkeypatch.setattr(steps, "function_error", lambda function, since: "ValueError: day is out of range for month")
    state = stage("2026-11-31")
    through_validator(state, aws)
    assert steps.deploy(state, "staging", aws.appconfig, KNOWN) == 0
    assert json.loads(aws.appconfig.served("staging")) == {KEY: "2026-11-31"}
    assert steps.check(state, "staging") == 1
    assert "The order page answered 502, 3 of 3 calls, in staging." in slack[-2]
    assert "> ValueError: day is out of range for month" in slack[-1]
    assert steps.back(state, "staging", client=aws.appconfig, known=KNOWN) == 0
    assert f"Staging is back on {BASELINE}." in slack[-1]
    assert json.loads(aws.appconfig.served("staging")) == {KEY: BASELINE}
    assert json.loads(aws.appconfig.served("production")) == {KEY: BASELINE}, "production was never touched"
    assert all(environment == "staging" for environment, _ in aws.functions.seen[2:])


def test_the_check_calls_the_broken_page_three_times_for_a_real_answer(stage, aws, slack):
    state = stage("2026-11-31")
    through_validator(state, aws)
    steps.deploy(state, "staging", aws.appconfig, KNOWN)
    aws.functions.seen.clear()
    steps.check(state, "staging")
    assert aws.functions.seen.count(("staging", "/")) == 3


def test_a_page_that_fails_once_fails_the_check_and_says_how_many(stage, aws, slack, monkeypatch):
    state = stage("2026-11-30")
    through_validator(state, aws)
    steps.deploy(state, "staging", aws.appconfig, KNOWN)
    answers = iter([(200, ""), (502, ""), (200, "")] + [(200, "")] * 3)

    def get(url, timeout=5):
        return aws.functions.get(url) if url.endswith("/health") else next(answers)

    monkeypatch.setattr(steps, "http_get", get)
    assert steps.check(state, "staging") == 1
    assert "The order page answered 502, 1 of 3 calls, in staging." in slack[-1]


def test_a_value_that_never_arrives_fails_the_check(stage, aws, slack, monkeypatch):
    state = stage("2026-11-30")
    through_validator(state, aws)
    steps.deploy(state, "staging", aws.appconfig, KNOWN)
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(steps, "clock", lambda: next(ticks))
    monkeypatch.setattr(steps, "http_get", lambda url, timeout=5: (200, '{"settings": {}}'))
    assert steps.check(state, "staging") == 1
    assert f"/health didn't report the new value in {steps.VALUE_SECONDS}s." in slack[-1]


def test_going_back_with_nothing_to_go_back_to_says_so(stage, aws, slack):
    state = stage("2026-11-31")
    state["setting"]["previous"] = {"staging": {"version": None, "value": ""}}
    assert steps.back(state, "staging", client=aws.appconfig, known=KNOWN) == 1
    assert "staging had no earlier version" in slack[-1]


def test_going_back_that_doesnt_show_says_so(stage, aws, slack, monkeypatch):
    state = stage("2026-11-31")
    through_validator(state, aws)
    steps.deploy(state, "staging", aws.appconfig, KNOWN)
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(steps, "clock", lambda: next(ticks))
    monkeypatch.setattr(steps, "http_get", lambda url, timeout=5: (200, '{"settings": {}}'))
    assert steps.back(state, "staging", client=aws.appconfig, known=KNOWN) == 1
    assert "doesn't say so yet" in slack[-1]


def test_a_staging_deployment_put_back_stops_the_run(stage, aws, slack, monkeypatch):
    state = stage("2026-11-30")
    through_validator(state, aws)
    monkeypatch.setattr(steps, "wait_done", lambda *args: {"State": "ROLLED_BACK"})
    assert steps.deploy(state, "staging", aws.appconfig, KNOWN) == 1
    assert "ended ROLLED_BACK" in slack[-1]


def test_a_busy_environment_says_so(stage, aws, slack):
    state = stage("2026-11-30")
    through_validator(state, aws)
    aws.appconfig.deployments["env-s"][0]["State"] = "BAKING"
    assert steps.deploy(state, "staging", aws.appconfig, KNOWN) == steps.REFUSED
    assert "still runs in staging" in slack[-1]


def test_another_deploy_error_is_raised(stage, aws, slack):
    state = stage("2026-11-30")
    through_validator(state, aws)

    def broken(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerException", "Message": "boom"}}, "StartDeployment")

    aws.appconfig.start_deployment = broken
    with pytest.raises(ClientError):
        steps.deploy(state, "staging", aws.appconfig, KNOWN)


def test_a_deployment_that_never_ends_raises(aws, monkeypatch):
    aws.appconfig.deployments["env-p"].insert(0, aws.appconfig.done(5, 1, "BAKING"))
    aws.appconfig.bakes_forever = True
    ticks = iter(range(0, 1000, 50))
    monkeypatch.setattr(steps, "clock", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        steps.wait_done(aws.appconfig, KNOWN, "production", 5, 100)


# The function's own words.

class FakeLogs:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def client(self, name):
        assert name == "logs"
        return self

    def get_paginator(self, name):
        assert name == "filter_log_events"
        return self

    def paginate(self, **kwargs):
        self.asked.append(kwargs)
        return self.pages


def test_the_exception_comes_from_the_functions_own_log():
    logs = FakeLogs([{"events": []}, {"events": [{"message": "START RequestId"}, {
        "message": "[ERROR] ValueError: day is out of range for month\nTraceback (most recent call last):\n  File ..."}]}])
    said = steps.function_error("orders-service-staging", 1000.5, session=logs)
    assert said == "ValueError: day is out of range for month"
    assert logs.asked == [{"logGroupName": "/aws/lambda/orders-service-staging", "startTime": 1000500, "filterPattern": "ERROR"}]


def test_no_exception_in_the_log_means_no_line(monkeypatch):
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(steps, "clock", lambda: next(ticks))
    assert steps.function_error("f", 1.0, session=FakeLogs([{"events": []}])) is None


def test_a_log_that_cant_be_read_means_no_line(capsys):
    class Refusing:
        def client(self, name):
            raise RuntimeError("denied")

    assert steps.function_error("f", 1.0, session=Refusing()) is None
    assert "wasn't read" in capsys.readouterr().out


# Try 3: through.

@pytest.fixture
def servicenow(monkeypatch):
    sn = FakeServiceNow()
    monkeypatch.setattr(changes, "connect", lambda: (sn, None))
    monkeypatch.setattr(changes, "requester_user", lambda sn, slack_id: {"sys_id": "sam1", "first_name": "Sam"})
    return sn


def test_third_try_goes_through_and_closes_with_the_before_and_after(stage, aws, slack, servicenow):
    state = stage("2026-11-30")
    through_validator(state, aws)
    assert steps.deploy(state, "staging", aws.appconfig, KNOWN) == 0
    assert steps.check(state, "staging") == 0
    steps.open_change(state)
    posted = next(body_ for method, path, body_ in servicenow.calls if method == "POST")
    assert posted["short_description"] == f"Setting {KEY} in orders-service production: {BASELINE} to 2026-11-30"
    assert "description" not in posted, "a standard change keeps its template's texts"
    assert state["change"]["type"] == "standard" and state["change"]["number"] == "CHG0030200"
    assert steps.deploy(state, "production", aws.appconfig, KNOWN) == 0
    assert state["deploys"]["production"]["number"] == "2"
    production = next(call for name, call in aws.appconfig.calls if name == "start" and call["EnvironmentId"] == "env-p")
    assert production["DeploymentStrategyId"] == "bake1" and production["ConfigurationVersion"] == str(state["setting"]["version"])
    assert steps.check(state, "production", client=aws.appconfig, known=KNOWN) == 0
    assert "Bake time: 2 minutes." in slack[-1]
    fake_finish(state, "live")
    steps.close_change(state)
    patched = [body_ for method, path, body_ in servicenow.calls if method == "PATCH" and body_.get("close_code")]
    assert patched[0]["close_code"] == "successful"
    notes = patched[0]["close_notes"]
    assert f"{KEY}: from {BASELINE} to 2026-11-30 in production." in notes
    assert "No build and no new version: orders-service 1.31.0 before and after." in notes
    assert "Total time: 5s." in notes


def test_the_same_version_goes_to_staging_and_to_production(stage, aws, slack):
    state = stage("2026-11-30")
    through_validator(state, aws)
    steps.deploy(state, "staging", aws.appconfig, KNOWN)
    steps.deploy(state, "production", aws.appconfig, KNOWN)
    versions = {call["EnvironmentId"]: call["ConfigurationVersion"] for name, call in aws.appconfig.calls if name == "start"}
    assert versions["env-s"] == versions["env-p"] == str(state["setting"]["version"])


def test_a_release_in_between_is_said_on_the_close_notes(stage, aws, slack, servicenow, monkeypatch):
    state = stage("2026-11-30")
    state["setting"].update(before=BASELINE, version=2, deployments={"production": 2}, live_version="1.30.0")
    state["change"] = {"sys_id": "chg1", "number": "CHG1", "type": "standard"}
    state["total"] = "3m 00s"
    steps.close_change(state)
    notes = [b["close_notes"] for m, p, b in servicenow.calls if m == "PATCH" and b.get("close_notes")][0]
    assert "went from 1.30.0 to 1.31.0 meanwhile" in notes


def test_no_template_stops_with_the_way_out(stage, slack, monkeypatch):
    state = stage("2026-11-30")
    state["setting"].update(before=BASELINE, version=2)
    monkeypatch.setattr(changes, "requester_user", lambda sn, slack_id: {"sys_id": "sam1"})
    with pytest.raises(SystemExit):
        steps.open_change(state, sn=FakeServiceNow(templates=()), ssm=None)


def test_a_failed_production_check_puts_the_value_back_and_says_where_it_stopped(stage, aws, slack):
    state = stage("2026-11-31")
    through_validator(state, aws)
    steps.deploy(state, "production", aws.appconfig, KNOWN)
    assert steps.check(state, "production", client=aws.appconfig, known=KNOWN) == 1
    assert ("stop", 2) in aws.appconfig.calls
    assert json.loads(aws.appconfig.served("production")) == {KEY: BASELINE}
    assert state["finish_line"] == f"Stopped after {{total}}. AWS AppConfig put {BASELINE} back in production."
    fake_finish(state, "failed")
    assert f"Stopped after 5s. AWS AppConfig put {BASELINE} back in production." in slack[-1]


def test_an_alarm_during_the_bake_says_what_put_it_back(stage, aws, slack, monkeypatch):
    state = stage("2026-11-30")
    through_validator(state, aws)
    steps.deploy(state, "production", aws.appconfig, KNOWN)
    monkeypatch.setattr(steps, "wait_done", lambda *args: {
        "State": "ROLLED_BACK", "EventLog": [{"EventType": "ROLLBACK_STARTED", "TriggeredBy": "CLOUDWATCH_ALARM"}]})
    assert steps.check(state, "production", client=aws.appconfig, known=KNOWN) == 1
    assert "AWS AppConfig rolled it back: orders-service-production-errors went into ALARM." in slack[-1]


def test_braces_in_a_value_cant_break_the_stop_line(stage, aws, slack):
    aws.appconfig.versions[1] = json.dumps({KEY: "{odd}"}).encode()
    state = stage("2026-11-31")
    through_validator(state, aws)
    steps.deploy(state, "production", aws.appconfig, KNOWN)
    steps.check(state, "production", client=aws.appconfig, known=KNOWN)
    fake_finish(state, "failed")
    assert "put {odd} back" in slack[-1]


# The three takes in order.

def test_the_three_takes_in_order(stage, aws, slack, servicenow):
    first = stage("31-11-2026")
    assert is_refused(first, aws)

    second = stage("2026-11-31")
    through_validator(second, aws)
    assert steps.deploy(second, "staging", aws.appconfig, KNOWN) == 0
    assert steps.check(second, "staging") == 1
    assert steps.back(second, "staging", client=aws.appconfig, known=KNOWN) == 0
    assert aws.functions.get("https://production.example/")[0] == 200
    assert "Free delivery until 27 November" in aws.functions.get("https://production.example/")[1]

    third = stage("2026-11-30")
    through_validator(third, aws)
    assert third["setting"]["before"] == BASELINE
    assert steps.deploy(third, "staging", aws.appconfig, KNOWN) == 0
    assert steps.check(third, "staging") == 0
    steps.open_change(third)
    assert steps.deploy(third, "production", aws.appconfig, KNOWN) == 0
    assert steps.check(third, "production", client=aws.appconfig, known=KNOWN) == 0
    page = aws.functions.get("https://production.example/")
    assert page[0] == 200 and "Free delivery until 30 November" in page[1] and "1.31.0" in page[1]
    assert json.loads(aws.functions.get("https://production.example/health")[1])["version"] == "1.31.0"


# The message.

def test_the_message_has_the_setting_head_its_own_rows_and_no_release_words(stage, slack):
    state = stage("2026-11-30")
    message = notify.render(state)
    assert message.startswith(f"*Setting change: {KEY}* · orders-service production · requested by <@{SAM}>\nTo 2026-11-30")
    for row in steps.STAGES:
        assert row in message
    assert "Release" not in message and "Build\n" not in message and "Test\n" not in message
    assert state["stages"].keys() == dict.fromkeys(steps.STAGES).keys()


def test_the_events_of_a_setting_stay_out_of_the_release_views(stage):
    import events

    state = stage("2026-11-30")
    args = type("Args", (), dict(stage="Staging", result="ok", seconds=1.0, environment="staging", deploy=False,
                                 recovery_seconds=None, job=None))()
    event = events.event(args, state)
    assert event["service"] == "orders-service-settings" and "deploy" not in event


# The command line, and where the addresses come from.

def base_state():
    return {"service": "orders-service", "version": "1.3.0", "jira_key": "", "jira_keys": [], "requested_by": SAM,
            "slack": {"channel": "C1", "ts": "1.1"}, "build": {"number": "7", "url": "u"}, "started_at": 1000.0, "stages": {}}


def test_init_takes_the_key_and_value_from_the_environment_only(monkeypatch, slack):
    run_state.save(base_state())
    monkeypatch.setenv("SETTING", KEY)
    monkeypatch.setenv("VALUE", "2026-11-30; rm -rf /")
    monkeypatch.setattr("sys.argv", ["setting.py", "init"])
    assert steps.main() == 0
    assert run_state.load()["setting"] == {"key": KEY, "value": "2026-11-30; rm -rf /"}


def test_a_refusal_is_exit_3_and_the_state_is_kept(monkeypatch, slack):
    run_state.save({**base_state(), "setting": {"key": KEY, "value": "x"}})

    def refuse(state):
        state["setting"]["before"] = BASELINE
        return steps.REFUSED

    monkeypatch.setattr(steps, "validate", refuse)
    monkeypatch.setattr("sys.argv", ["setting.py", "validate"])
    assert steps.main() == steps.REFUSED
    assert run_state.load()["setting"]["before"] == BASELINE


def test_the_address_comes_from_the_deploy_role_once(monkeypatch):
    import deploy

    calls = []

    class Client:
        def get_function_url_config(self, FunctionName, Qualifier):
            calls.append((FunctionName, Qualifier))
            return {"FunctionUrl": "https://staging.example/"}

    monkeypatch.setattr(deploy, "lambda_client", lambda service_name: Client())
    state = {**base_state(), "setting": {"key": KEY, "value": "x"}}
    assert steps.function_url(state, "staging") == "https://staging.example"
    assert steps.function_url(state, "staging") == "https://staging.example"
    assert calls == [("orders-service-staging", "live")]


def test_status_says_what_each_environment_has_and_serves(aws, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("settings_demo", steps.HERE / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    class Functions:
        def get_function_url_config(self, FunctionName, Qualifier):
            return {"FunctionUrl": f"https://{FunctionName.split('-')[-1]}.example/"}

    monkeypatch.setattr(steps, "appconfig", lambda: aws.appconfig)
    monkeypatch.setattr(demo, "aws_session", lambda: type("Session", (), {"client": lambda self, name: Functions()})())
    demo.status()
    out = capsys.readouterr().out
    assert f'staging: version 1 of the file: {{"free_delivery_until": "{BASELINE}"}}' in out
    assert "orders-service-production runs 1.31.0" in out
