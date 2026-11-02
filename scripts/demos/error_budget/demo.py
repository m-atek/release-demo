"""scripts/demo.sh up, down, status and check for video 27, the error budget.

    python scripts/demos/error_budget/demo.py up       the traffic on, for the next 72 hours at most
    python scripts/demos/error_budget/demo.py down     the traffic and the check off
    python scripts/demos/error_budget/demo.py status   the switch, the traffic's last minute, the numbers, what's live and staged
    python scripts/demos/error_budget/demo.py check    the same as ok or FAIL lines

The traffic and the check have two switches on purpose: with the traffic on and
the check off, other orders-service videos can be filmed during the fill. up
touches only the traffic, so a forgotten demo stops sending within 3 days. It
applies nothing: scripts/demos/error_budget/tf.sh apply holds IAM and is yours.
"""

import argparse
import base64
import datetime
import importlib.util
import json
import sys
import time
import urllib.request

import budget
from common import SSM_PREFIX, aws_session, put_parameter, service, ssm_client

SERVICE = "orders-service"
FUNCTION = "release-demo-error-budget-traffic"
TRAFFIC = "error-budget/traffic-until"
UP_HOURS = 72
# Acceptance check 2: 60 a minute, give or take 2.
PER_MINUTE = range(58, 63)


def load_switch():
    spec = importlib.util.spec_from_file_location("budget_switch", budget.HERE / "switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SWITCH


def up(now=None):
    client = aws_session().client("lambda")
    try:
        client.get_function_configuration(FunctionName=FUNCTION)
    except client.exceptions.ResourceNotFoundException:
        print(f"{FUNCTION} isn't there yet: scripts/demos/error_budget/tf.sh apply first. It holds IAM, so you apply it.")
        return 1
    now = now or time.time()
    until = datetime.datetime.fromtimestamp(now + UP_HOURS * 3600, datetime.timezone.utc)
    put_parameter(ssm_client(), TRAFFIC, until.isoformat(timespec="seconds"), secure=False)
    print(f"Traffic on since {budget.clock(now)}, Amsterdam time, until {until:%d %b %H:%M} UTC at the latest. "
          "The takes start at least 24 hours from now, and after the rehearsal's last failure. "
          "Note the time. The first requests come within a minute; scripts/demo.sh check error-budget confirms them.")
    return 0


def down():
    put_parameter(ssm_client(), TRAFFIC, "off", secure=False)
    load_switch().set("off")
    print("Traffic off: within a minute only the pipeline's own calls are left.")
    return 0


def live_and_staged(session):
    """What production runs and what main has staged, each or why not."""
    config = service(SERVICE)
    function = config["environments"]["production"]["function"]
    try:
        client = session.client("lambda")
        number = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
        live = client.get_function_configuration(FunctionName=function, Qualifier=number).get("Description") or "?"
    except Exception as error:
        live = f"not read ({error.__class__.__name__})"
    try:
        staged = main_version(config["path"])
    except (Exception, SystemExit) as error:
        staged = f"not read ({error.__class__.__name__})"
    return live, staged


def main_version(path):
    """The VERSION on main, from GitHub's public API: a status needs no key. The repository's name isn't secret."""
    repository = plain_parameter("github/repository")
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/contents/{path}/VERSION?ref=main",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "release-demo/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return base64.b64decode(json.loads(response.read())["content"]).decode().strip()


def status():
    session = aws_session()
    settings = budget.settings_of(SERVICE)
    print(f"Check's switch: {load_switch().value() or 'not readable'}")
    print(f"Traffic until: {plain_parameter(TRAFFIC)}")
    reading = budget.take_reading(session.client("cloudwatch"), settings, time.time())
    minute, requests, failed = reading.series[-1]
    print(f"The traffic's last minute, {budget.clock(minute)}: {requests} requests, {failed} failed")
    print(budget.head(reading, f"Spent: {reading.spent}%.{budget.back_about(reading, time.time())}"))
    live, staged = live_and_staged(session)
    print(f"Live in production: {live}. Staged on main: {staged}.")
    return 0


def checks(session=None, now=None):
    """[(ok, what)] for everything the demo needs, read only."""
    session = session or aws_session()
    now = now or time.time()
    settings = budget.settings_of(SERVICE)
    found = []
    try:
        session.client("lambda").get_function_configuration(FunctionName=FUNCTION)
        found.append((True, f"function {FUNCTION}"))
    except Exception as error:
        found.append((False, f"function {FUNCTION}: not there ({error.__class__.__name__}): tf.sh apply"))
    try:
        rule = session.client("events").describe_rule(Name=FUNCTION)
        found.append((rule["State"] == "ENABLED", f"rule {FUNCTION}: {rule['State']}, {rule['ScheduleExpression']}"))
    except Exception as error:
        found.append((False, f"rule {FUNCTION}: not there ({error.__class__.__name__}): tf.sh apply"))
    switch = load_switch().value()
    found.append((switch in ("on", "off", "unset"), f"switch error-budget/enabled: {switch or 'not readable'}"))
    until = plain_parameter(TRAFFIC)
    try:
        on = datetime.datetime.fromisoformat(until).timestamp() > now
    except (TypeError, ValueError):
        on = False
    found.append((on, f"traffic {'on' if on else 'off'} (error-budget/traffic-until: {until})"))
    iam = session.client("iam")
    found.append(policy_has(iam, "release-demo-evidence-reader", "read-orders-service-records", "cloudwatch:GetMetricData"))
    found.append(policy_has(iam, "release-demo-tools-host", "error-budget-switch", "ssm:PutParameter"))
    try:
        reading = budget.take_reading(session.client("cloudwatch"), settings, now)
        last = reading.series[-1]
        found.append((last[1] in PER_MINUTE, f"last whole minute: {last[1]} requests (60, give or take 2), {last[2]} failed"))
        found.append((True, f"window: {reading.failed:,} of {reading.requests:,} failed, allows {reading.allowed:,}, spent {reading.spent}%"))
    except Exception as error:
        found.append((False, f"CloudWatch not read ({error.__class__.__name__})"))
    found.append(url5xx_line(session, settings, now))
    live, staged = live_and_staged(session)
    found.append((True, f"live in production: {live}; staged on main: {staged}"))
    return found


def plain_parameter(name):
    """A plain String from SSM, or None if it can't be read. Nothing here decrypts anything."""
    try:
        return ssm_client().get_parameter(Name=SSM_PREFIX + name)["Parameter"]["Value"]
    except (Exception, SystemExit):
        return None


def policy_has(iam, role, policy, action):
    try:
        document = iam.get_role_policy(RoleName=role, PolicyName=policy)["PolicyDocument"]
    except Exception as error:
        return False, f"{role}'s policy {policy}: not read ({error.__class__.__name__}): your apply"
    return action in str(document), f"{role} {'may' if action in str(document) else 'may not'} {action}"


def url5xx_line(session, settings, now):
    """An open question from the build: does Url5xxCount ever appear? It decides nothing while services.yaml reads Errors."""
    try:
        metrics = session.client("cloudwatch").list_metrics(
            Namespace="AWS/Lambda", MetricName="Url5xxCount",
            Dimensions=[{"Name": "FunctionName", "Value": settings["function"]}])["Metrics"]
    except Exception as error:
        return False, f"Url5xxCount not read ({error.__class__.__name__})"
    return True, (f"Url5xxCount on {settings['function']}: {'published' if metrics else 'not published'}; "
                  f"services.yaml reads {budget.metrics_of(settings)['failed']}")


def check():
    found = checks()
    for ok, what in found:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
    return 0 if all(ok for ok, _ in found) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status", "check"])
    args = parser.parse_args()
    return {"up": up, "down": down, "status": status, "check": check}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
