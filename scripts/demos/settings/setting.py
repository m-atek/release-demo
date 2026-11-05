#!/usr/bin/env python3
"""One setting through AWS AppConfig, with no build: the steps of the job /setting starts (video 29).

    python3 scripts/demos/settings/setting.py init                the run state becomes a setting's (SETTING, VALUE)
    python3 scripts/demos/settings/setting.py validate            a new version of the file, and AWS's validator
    python3 scripts/demos/settings/setting.py deploy staging      all at once; waits until it completes
    python3 scripts/demos/settings/setting.py check staging       the new value live, every path answers
    python3 scripts/demos/settings/setting.py back staging        what it replaced, back, and live again
    python3 scripts/demos/settings/setting.py change              the pre-approved change, with the before and after
    python3 scripts/demos/settings/setting.py deploy production   all at once, with bake time
    python3 scripts/demos/settings/setting.py check production    the value live, every path answers, then the bake
    python3 scripts/demos/settings/setting.py close               the change closes, with the before and after

jenkins/shared-library/vars/settingPipeline.groovy calls these in order, as the
Jenkins job setting, which the Slack handler starts as release-bot. The rows and
lines go in the message the handler posted, through notify.py. Nothing here
builds or publishes: orders-service keeps the Lambda version it has.

A file AWS refuses (exit 3) stores a version nobody deploys. The next change
starts from what production last completed, never from the newest version, so
a refused value can't ride along with the next one.

The ids come from SSM (appconfig/orders-service-settings), which
infra/demos/29-one-setting writes; that root also lets the tools host's role read
this application's versions and deployments, and deploy them.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))

import notify  # noqa: E402
import run_state  # noqa: E402
from common import fail, get_parameter, http_get, service, ssm_client  # noqa: E402

STAGES = ["AWS validator", "Staging", "Check staging", "Change", "Approval", "Production", "Check production", "Record"]
REFUSED = 3                 # exit code: AWS refused the file, or there is nothing to change
CALLS, BETWEEN_CALLS = 3, 3 # each path, three times, 3 seconds apart: one failure fails it, and says how many
VALUE_SECONDS = 45          # a container reads the file at most every 15 seconds; then AWS's and the request's own time
DEPLOY_SECONDS = 120        # a deployment with no bake time completes in seconds
BAKE_MARGIN_SECONDS = 180   # on top of the bake time, before a run gives up on AWS
LOG_SECONDS = 12            # CloudWatch Logs takes some seconds to show a line
WORDS_LIMIT = 300
# AWS's answer goes on screen as it is, apart from these: no ARN and no account id in Slack.
NOT_ON_SCREEN = re.compile(r"arn:aws[\w:/.*+=,@-]*|\b\d{12}\b")
# Lambda's text log of an unhandled exception: "[ERROR] ValueError: day is out of range for month".
ERROR_LINE = re.compile(r"\[ERROR\]\s+(\w[\w.]*:\s.+)")
FINISHED_BADLY = ("ROLLED_BACK", "REVERTED")
PATH_NAMES = {"/": "The order page"}    # how Slack names a path


def pause(seconds):
    time.sleep(seconds)


def clock():
    return time.monotonic()


def flags_module():
    """Demo 21's AppConfig helpers, by path: their folder's name has a dash, and the helpers take the ids they're given."""
    spec = importlib.util.spec_from_file_location("release_train_flags", SCRIPTS / "demos" / "release-train" / "flags.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flags = flags_module()


def settings_of(service_name):
    config = service(service_name).get("app_settings")
    if not config:
        fail(f"{service_name} has no app_settings in services.yaml.")
    return config


def ids(service_name):
    return json.loads(get_parameter(ssm_client(), f"appconfig/{service_name}-settings"))


def appconfig():
    return flags.client()


def escaped(text):
    """Slack reads <, > and & as its own markup."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def say(state, *lines):
    """Lines under the stages in the run's Slack message, and the run state saved, whatever comes next."""
    state.setdefault("lines", []).extend(lines)
    run_state.save(state)
    notify.post(state)


def head(state):
    """The message's first two lines. The second gives the before and after once the before is known."""
    setting = state["setting"]
    top = (f"*Setting change: {setting['key']}* · {state['service']} production · "
           f"requested by {notify.requester(state.get('requested_by'))}")
    if "before" in setting:
        return f"{top}\nFrom {escaped(setting['before'])} to {escaped(setting['value'])}"
    return f"{top}\nTo {escaped(setting['value'])}"


def init(state, key, value):
    """Makes the run state a setting's: no Jira issue, its own rows, and its events apart from the releases'."""
    config = settings_of(state["service"])
    if key not in config["keys"]:
        fail(f"{state['service']} has no setting {key}. Its settings: {', '.join(config['keys'])}.")
    state.update(kind="setting", jira_key="", jira_keys=[], option="setting", events_service=f"{state['service']}-settings",
                 setting={"key": key, "value": value}, stages={name: {"status": "waiting"} for name in STAGES})
    state["head"] = head(state)
    run_state.save(state)
    notify.post(state)
    print(f"Setting {key} of {state['service']}: {value!r}")


# What AWS AppConfig holds.

def current(client, known, environment):
    """(version number, the file) the environment's last completed deployment put there; (None, {}) before any."""
    for item in flags.deployments(client, known, environment):
        if item["State"] == "COMPLETE":
            number = int(item["ConfigurationVersion"])
            return number, flags.content_of(client, known, number)
    return None, {}


def aws_words(error):
    """What AWS said, as it said it: each reason in the answer's details, or else its message."""
    reply = error.response
    details = (reply.get("Details") or {}).get("InvalidConfiguration") or []
    reasons = [detail.get("Reason") or detail.get("Constraint") or detail.get("Value") for detail in details]
    words = [reason for reason in reasons if reason] or [reply.get("Error", {}).get("Message") or str(error)]
    return [NOT_ON_SCREEN.sub("...", word)[:WORDS_LIMIT] for word in words]


def error_code(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code", "")


def refused(state, error):
    """AWS said no: its own words in Slack, and everything it said in the build's log."""
    print(f"AWS AppConfig's whole answer: {json.dumps(error.response, default=str)}")
    version = state["setting"].get("version")
    named = f"version {version} of the settings file" if version else "the settings file"
    say(state, f"AWS AppConfig refused {named}:", *(f"> {escaped(word)}" for word in aws_words(error)))
    return REFUSED


def busy(state, environment):
    say(state, f"An AWS AppConfig deployment still runs in {environment}. Try again when it ends.")
    return REFUSED


def live_health(url):
    """/health's answer as a dict, {} when it doesn't answer."""
    status, body = http_get(f"{url}/health", timeout=10)
    if status != 200:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        return {}


def function_url(state, environment):
    urls = state["setting"].setdefault("urls", {})
    if environment not in urls:
        from deploy import lambda_client

        function = service(state["service"])["environments"][environment]["function"]
        urls[environment] = lambda_client(state["service"]).get_function_url_config(
            FunctionName=function, Qualifier="live")["FunctionUrl"].rstrip("/")
        run_state.save(state)
    return urls[environment]


# The stages.

def validate(state, client=None, known=None):
    """The new version of the file, from what production has, with one value changed, and AWS's validator on it."""
    from botocore.exceptions import ClientError

    client, known = client or appconfig(), known or ids(state["service"])
    setting = state["setting"]
    key, value = setting["key"], setting["value"]
    for environment in ("staging", "production"):
        health = live_health(function_url(state, environment))
        if "settings" not in health:
            what = (f"{state['service']} {health['version']} doesn't read settings" if health else
                    f"{state['service']} doesn't answer on /health")
            say(state, f"{what} in {environment}. Nothing changed.")
            return REFUSED
        state["version"] = setting["live_version"] = health.get("version", state["version"])
    _, content = current(client, known, "production")
    setting["before"] = content.get(key, "")
    state["head"] = head(state)
    if setting["before"] == value:
        say(state, f"{key} is {escaped(value)} in production already. Nothing to change.")
        return REFUSED
    run_state.save(state)
    try:
        made = client.create_hosted_configuration_version(
            ApplicationId=known["application"], ConfigurationProfileId=known["profile"],
            Content=json.dumps({**content, key: value}, indent=2).encode(), ContentType="application/json",
            LatestVersionNumber=flags.latest_version(client, known),
            Description=f"{key}: /setting in Slack, build {state['build'].get('number') or 'by hand'}")
        setting["version"] = made["VersionNumber"]
        run_state.save(state)
        client.validate_configuration(ApplicationId=known["application"], ConfigurationProfileId=known["profile"],
                                      ConfigurationVersion=str(made["VersionNumber"]))
    except ClientError as error:
        if error_code(error) == "BadRequestException":
            return refused(state, error)
        raise
    print(f"AWS's validator passed version {setting['version']}: {key} from {setting['before']!r} to {value!r}")
    return 0


def start(client, known, environment, version, strategy, description):
    return client.start_deployment(
        ApplicationId=known["application"], EnvironmentId=known["environments"][environment],
        DeploymentStrategyId=known[strategy], ConfigurationProfileId=known["profile"],
        ConfigurationVersion=str(version), Description=description)["DeploymentNumber"]


def wait_done(client, known, environment, number, timeout):
    """The deployment as it ended: COMPLETE, or put back. Raises when AWS takes longer than timeout."""
    deadline = clock() + timeout
    while True:
        deployment = client.get_deployment(ApplicationId=known["application"], EnvironmentId=known["environments"][environment],
                                            DeploymentNumber=number)
        if deployment["State"] == "COMPLETE" or deployment["State"] in FINISHED_BADLY:
            return deployment
        if clock() >= deadline:
            raise TimeoutError(f"AWS AppConfig deployment {number} in {environment} is still {deployment['State']}.")
        pause(2)


def deploy(state, environment, client=None, known=None):
    """Staging: all at once, and it waits. Production: all at once with bake time, and the check waits for that."""
    from botocore.exceptions import ClientError

    client, known = client or appconfig(), known or ids(state["service"])
    setting = state["setting"]
    number, content = current(client, known, environment)
    setting.setdefault("previous", {})[environment] = {"version": number, "value": content.get(setting["key"], "")}
    run_state.save(state)
    strategy = "bake" if environment == "production" else "quick"
    try:
        deployment = start(client, known, environment, setting["version"], strategy,
                           f"{setting['key']}: /setting in Slack, build {state['build'].get('number') or 'by hand'}")
    except ClientError as error:
        code = error_code(error)
        if code == "BadRequestException":
            return refused(state, error)
        if code == "ConflictException":
            return busy(state, environment)
        raise
    setting.setdefault("deployments", {})[environment] = deployment
    if environment == "production":
        # notify.py and the pipeline's stop read this: from here on, production has it.
        state.setdefault("deploys", {})["production"] = {"number": str(deployment), "appconfig": True}
        run_state.save(state)
        print(f"AWS AppConfig deployment {deployment} in production started")
        return 0
    run_state.save(state)
    ended = wait_done(client, known, environment, deployment, DEPLOY_SECONDS)
    if ended["State"] != "COMPLETE":
        say(state, f"AWS AppConfig deployment {deployment} in {environment} ended {ended['State']}.")
        return 1
    print(f"AWS AppConfig deployment {deployment} in {environment} complete")
    return 0


def wait_for_value(url, key, value, timeout):
    deadline = clock() + timeout
    while True:
        if live_health(url).get("settings", {}).get(key) == value:
            return True
        if clock() >= deadline:
            return False
        pause(2)


def path_answers(url, path):
    """The status of each of a path's calls."""
    answers = []
    for call in range(CALLS):
        if call:
            pause(BETWEEN_CALLS)
        answers.append(http_get(url + path, timeout=10)[0])
    return answers


def function_error(function, since, session=None):
    """The exception the function logged since then, in Python's own words. None when there is none or
    the log can't be read: the check has failed either way."""
    try:
        if session is None:
            from evidence import reader_session

            session = reader_session()
        pages = session.client("logs").get_paginator("filter_log_events")
        deadline = clock() + LOG_SECONDS
        while True:
            for page in pages.paginate(logGroupName=f"/aws/lambda/{function}", startTime=int(since * 1000), filterPattern="ERROR"):
                for event in page["events"]:
                    found = ERROR_LINE.search(event["message"])
                    if found:
                        return found.group(1).strip()
            if clock() >= deadline:
                return None
            pause(2)
    except Exception as error:  # no line in Slack; the failed paths still say what happened
        print(f"The function's log wasn't read: {error.__class__.__name__}")
        return None


def check(state, environment, client=None, known=None):
    """The new value live in that environment, then every path answers 200, each of its calls.
    In production it then waits out the bake, and AWS AppConfig puts the value back if anything fails."""
    setting = state["setting"]
    config = settings_of(state["service"])
    key, value = setting["key"], setting["value"]
    url = function_url(state, environment)
    since = run_state.now()
    failed = None
    if not wait_for_value(url, key, value, VALUE_SECONDS):
        print(f"FAIL /health doesn't report {key} = {value!r} after {VALUE_SECONDS}s")
        failed = f"/health didn't report the new value in {VALUE_SECONDS}s."
    else:
        print(f"ok   /health reports {key} = {value!r}")
        for path in config["check_paths"]:
            answers = path_answers(url, path)
            bad = [status for status in answers if status != 200]
            print(f"{'FAIL' if bad else 'ok  '} {path} answered {', '.join(str(status or 'nothing') for status in answers)}")
            if bad and not failed:
                failed = f"{PATH_NAMES.get(path, path)} answered {bad[0] or 'nothing'}, {len(bad)} of {len(answers)} calls, in {environment}."
    if failed:
        say(state, failed)
        function = service(state["service"])["environments"][environment]["function"]
        said = function_error(function, since)
        if said:
            say(state, f"> {escaped(said)}")
    if environment != "production":
        return 1 if failed else 0
    client, known = client or appconfig(), known or ids(state["service"])
    number = setting["deployments"]["production"]
    if failed:
        return put_back(state, client, known, number)
    minutes = config["bake_minutes"]
    say(state, f"Bake time: {minutes} minutes.")
    ended = wait_done(client, known, "production", number, minutes * 60 + BAKE_MARGIN_SECONDS)
    if ended["State"] != "COMPLETE":
        return rolled_back(state, ended)
    print(f"AWS AppConfig deployment {number} in production complete")
    return 0


def put_back(state, client, known, number):
    """A failed production check: AWS AppConfig stops the deployment, which puts the last version back."""
    client.stop_deployment(ApplicationId=known["application"], EnvironmentId=known["environments"]["production"],
                           DeploymentNumber=number)
    ended = wait_done(client, known, "production", number, DEPLOY_SECONDS)
    return rolled_back(state, ended)


def rolled_back(state, ended):
    """AWS AppConfig put production's value back: on its alarm, or because the check stopped the deployment."""
    setting = state["setting"]
    before = escaped(setting["previous"]["production"]["value"])
    by_alarm = any(event.get("TriggeredBy") == "CLOUDWATCH_ALARM" for event in ended.get("EventLog") or [])
    if by_alarm:
        say(state, f"AWS AppConfig rolled it back: {settings_of(state['service'])['alarm']} went into ALARM.")
    safe = before.replace("{", "{{").replace("}", "}}")
    state["finish_line"] = f"Stopped after {{total}}. AWS AppConfig put {safe} back in production."
    run_state.save(state)
    return 1


def back(state, environment, client=None, known=None):
    """After a failed check in staging: what it replaced, deployed again, and seen live."""
    client, known = client or appconfig(), known or ids(state["service"])
    setting = state["setting"]
    previous = setting["previous"][environment]
    if previous["version"] is None:
        say(state, f"{environment} had no earlier version of the file to go back to. Look at it.")
        return 1
    deployment = start(client, known, environment, previous["version"], "quick",
                       f"{setting['key']}: back, after a failed check, build {state['build'].get('number') or 'by hand'}")
    wait_done(client, known, environment, deployment, DEPLOY_SECONDS)
    url = function_url(state, environment)
    if not wait_for_value(url, setting["key"], previous["value"], VALUE_SECONDS):
        say(state, f"{environment.capitalize()} was sent {escaped(previous['value'])} again, and /health doesn't say so yet. Look at it.")
        return 1
    say(state, f"{environment.capitalize()} is back on {escaped(previous['value'])}.")
    return 0


def open_change(state, sn=None, ssm=None):
    """The pre-approved change. A standard change keeps its template's description and plans whatever it is
    sent, so the before and after go in its short description, its notes and its close notes."""
    import change as changes

    if sn is None:
        sn, ssm = changes.connect()
    config = service(state["service"])
    setting = state["setting"]
    name = config["servicenow"]["templates"]["setting"]
    template = sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id")
    if template is None:
        fail(f"ServiceNow has no active standard change template '{name}'. Run scripts/servicenow_setup.py.")
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")["sys_id"]
    item = sn.find("cmdb_ci_appl", f"name={config['servicenow']['cmdb_item']}", "sys_id")["sys_id"]
    asked_by = changes.requester_user(sn, state.get("requested_by"))
    started = run_state.now()
    fields = {
        "short_description": before_and_after(state),
        "cmdb_ci": item, "assignment_group": group, "requested_by": asked_by["sys_id"], "category": "Software",
        "start_date": run_state.iso(started).replace("T", " ").rstrip("Z"),
        "end_date": run_state.iso(started + 1800).replace("T", " ").rstrip("Z"),
    }
    reply = sn.call("POST", f"/api/sn_chg_rest/change/standard/{template['sys_id']}", fields)
    change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
    state["change"] = {"sys_id": change_id, "number": number, "type": "standard",
                       "url": f"{sn.url}/change_request.do?sys_id={change_id}", "requested_by": asked_by["sys_id"]}
    run_state.save(state)
    changes.note(sn, state, "Stages so far:\n" + changes.stage_summary(state) +
                 f"\n\nAWS AppConfig version {setting['version']} of the settings file. Build: {state['build'].get('url', '')}")
    print(f"{number}: standard, pre-approved")


def before_and_after(state):
    setting = state["setting"]
    return f"Setting {setting['key']} in {state['service']} production: {setting['before']} to {setting['value']}"


def close_change(state, sn=None):
    """The change closes with the before and after, and says whether the code stayed what it was."""
    import change as changes

    if sn is None:
        sn, _ = changes.connect()
    setting = state["setting"]
    now = live_health(function_url(state, "production")).get("version", "unknown")
    same = (f"No build and no new version: {state['service']} {now} before and after."
            if now == setting.get("live_version") else
            f"{state['service']} went from {setting.get('live_version')} to {now} meanwhile, through a release of its own.")
    changes.close(sn, state, "successful",
                  f"{setting['key']}: from {setting['before']} to {setting['value']} in production. "
                  f"AWS AppConfig version {setting['version']}, deployment {setting['deployments']['production']}. "
                  f"{same} Total time: {state.get('total', 'unknown')}. Build: {state['build'].get('url', '')}")
    print(f"{state['change']['number']} closed: {before_and_after(state)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "validate", "change", "close"):
        sub.add_parser(command)
    for command in ("deploy", "check", "back"):
        sub.add_parser(command).add_argument("environment", choices=["staging", "production"])
    args = parser.parse_args()

    state = run_state.load()
    if args.command == "init":
        init(state, os.environ["SETTING"], os.environ["VALUE"])
        return 0
    code = {"validate": lambda: validate(state),
            "deploy": lambda: deploy(state, args.environment),
            "check": lambda: check(state, args.environment),
            "back": lambda: back(state, args.environment),
            "change": lambda: open_change(state),
            "close": lambda: close_change(state)}[args.command]()
    run_state.save(state)
    return code or 0


if __name__ == "__main__":
    sys.exit(main())
