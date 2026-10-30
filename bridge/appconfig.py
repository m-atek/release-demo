"""The event bridge's AWS AppConfig route (demo 21): every flag deployment in
orders-service production, recorded as a change, in Slack and in Grafana.

Amazon EventBridge brings AppConfig's deployment events here: the extension
"AppConfig deployment events to Amazon EventBridge" on the application sends
one when a deployment starts, completes or is rolled back, and a rule
(infra/demos/21-release-train-flags) passes production's on. For each, this
reads the deployment (GetDeployment: its description, configuration version
and event log) and that version's flag values, and writes, with the times in
Europe/Amsterdam:

- start: a standard change from the template "Feature flag change of
  orders-service", "Flag <flag> <on|off> in orders-service production",
  requested by the person the deployment's description names, put in
  Implement; a new Slack message in #releases with the change number; a
  Grafana annotation "flag <flag> <on|off> (<person>)";
- complete: the change closed successful, Slack's line "Done: <flag> is
  <on|off> in production. No alarm in 10 minutes.", an annotation;
- rolled back: when and why, from the event log and CloudWatch's alarm
  history; the first change closed unsuccessful; a second standard change for
  the switch back, opened and closed at once; Slack's line "Rolled back by
  alarm <alarm> at <time>, <N>s after it fired. <flag> is off again.
  <second change>"; an annotation.

A deployment whose description says reset.py is recorded the same way,
requested by release.pipeline, so production's flags never change off the
record. What later events need (the change, the Slack message) is kept on
the deployment itself, as tags, so a retried event records nothing twice.

Standard library and boto3 only, both in Lambda's Python runtime, so the
package is this file plus config.json, which Terraform writes. Secrets come
from SSM and are never logged.
"""

import base64
import datetime
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

SSM_PREFIX = "/release-demo/"
USER_AGENT = "release-demo-event-bridge/1.0 (+https://github.com/m-atek/release-demo)"
PARAMETERS = ["slack/bot-token", "slack/channel-id", "servicenow/instance-url",
              "servicenow/release-pipeline-password", "grafana/stack-url", "grafana/service-account-token"]
SCHEDULED, IMPLEMENT, REVIEW, CLOSED = "-2", "-1", "0", "3"
PIPELINE = "release.pipeline"

_clients = {}


def aws(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def config():
    return json.loads((Path(__file__).parent / "config.json").read_text())


def parameters():
    reply = aws("ssm").get_parameters(Names=[SSM_PREFIX + name for name in PARAMETERS], WithDecryption=True)
    return {p["Name"].removeprefix(SSM_PREFIX): p["Value"] for p in reply["Parameters"]}


# Times.

def amsterdam(moment):
    """A time in Europe/Amsterdam: CEST from the last Sunday of March to the last
    Sunday of October, at 01:00 UTC, CET otherwise, where zoneinfo has no data."""
    moment = moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo("Europe/Amsterdam"))
    except Exception:
        def last_sunday(month):
            day = datetime.datetime(moment.year, month, 31, 1, tzinfo=datetime.timezone.utc)
            return day - datetime.timedelta(days=(day.weekday() + 1) % 7)

        summer = last_sunday(3) <= moment.astimezone(datetime.timezone.utc) < last_sunday(10)
        return moment.astimezone(datetime.timezone(datetime.timedelta(hours=2 if summer else 1)))


def clock(moment):
    return amsterdam(moment).strftime("%H:%M:%S")


def servicenow_time(moment):
    """ServiceNow's date-time fields take UTC, as change.py writes them."""
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# The deployment.

def flag_states(deployment):
    """{flag: on|off} in the configuration version the deployment carries."""
    content = aws("appconfig").get_hosted_configuration_version(
        ApplicationId=deployment["ApplicationId"], ConfigurationProfileId=deployment["ConfigurationProfileId"],
        VersionNumber=int(deployment["ConfigurationVersion"]))["Content"].read()
    values = json.loads(content).get("values", {})
    return {name: "on" if (value or {}).get("enabled") else "off" for name, value in values.items()}


def switched(deployment, tags):
    """(flag, on|off): the flag /flag or reset.py named in the tags, else the first flag."""
    states = flag_states(deployment)
    name = tags.get("flag") or next(iter(sorted(states)), "flags")
    return name, states.get(name, tags.get("state", "off"))


def requester(deployment, tags, people):
    """(ServiceNow user name, how Slack names them, display name) from the description."""
    description = deployment.get("Description") or ""
    if "reset.py" in description:
        return PIPELINE, "reset.py", "reset.py"
    if tags.get("requested_by") == "terraform":
        return PIPELINE, "Terraform", "Terraform"
    for display, person in people.items():
        if description.startswith(display):
            mention = f"<@{tags['requested_by']}>" if re.fullmatch(r"[UW][A-Z0-9]{8,}", tags.get("requested_by", "")) \
                else display
            return person.get("servicenow_user") or PIPELINE, mention, display
    return PIPELINE, description or "AWS AppConfig", description or "AWS AppConfig"


def event_time(deployment, *types):
    """(when, by whom) of the first of these event types the deployment's log has, in the order given."""
    for kind in types:
        for entry in deployment.get("EventLog", []):
            if entry["EventType"] == kind:
                return entry["OccurredAt"], entry.get("TriggeredBy", "")
    return None, ""


def alarm_time(alarm, before, since):
    """When the alarm last went into ALARM, at or before the rollback."""
    reply = aws("cloudwatch").describe_alarm_history(
        AlarmName=alarm, HistoryItemType="StateUpdate", StartDate=since - datetime.timedelta(minutes=2),
        EndDate=before + datetime.timedelta(seconds=5), ScanBy="TimestampDescending", MaxRecords=50)
    for item in reply.get("AlarmHistoryItems", []):
        data = json.loads(item.get("HistoryData") or "{}")
        if data.get("newState", {}).get("stateValue") == "ALARM" and item["Timestamp"] <= before:
            return item["Timestamp"]
    return None


# ServiceNow, as release.pipeline.

class ServiceNow:
    def __init__(self, url, password):
        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{PIPELINE}:{password}".encode()).decode()

    def call(self, method, path, body=None, **query):
        query = {k: v for k, v in query.items() if v is not None}
        request = urllib.request.Request(
            f"{self.url}{path}{'?' + urllib.parse.urlencode(query) if query else ''}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"ServiceNow {method} {path}: {error.code} {error.read().decode(errors='replace')[:200]}")
        if "<html" in raw.lower():
            raise RuntimeError("ServiceNow is asleep: wake it at developer.servicenow.com")
        return json.loads(raw)["result"] if raw else None

    def find(self, table, query, fields="sys_id"):
        rows = self.call("GET", f"/api/now/table/{table}", sysparm_query=query, sysparm_limit=1, sysparm_fields=fields)
        return rows[0] if rows else None

    def open(self, settings, fields, requested_by, started, minutes=10):
        """A pre-approved change from the flag template, in Implement from started. Returns (sys_id, number)."""
        template = self.find("std_change_record_producer", f"name={settings['template']}^active=true")
        if template is None:
            raise RuntimeError(f"ServiceNow has no template '{settings['template']}': run scripts/servicenow_setup.py.")
        ci = self.find("cmdb_ci_appl", f"name={settings['service']}")
        group = self.find("sys_user_group", "name=Platform team")
        user = self.find("sys_user", f"user_name={requested_by}") or self.find("sys_user", f"user_name={PIPELINE}")
        fields = {**fields, "cmdb_ci": ci["sys_id"] if ci else "", "assignment_group": group["sys_id"] if group else "",
                  "requested_by": user["sys_id"], "category": "Software", "start_date": servicenow_time(started),
                  "end_date": servicenow_time(started + datetime.timedelta(minutes=minutes))}
        reply = self.call("POST", f"/api/sn_chg_rest/change/standard/{template['sys_id']}", fields)
        change_id, number = reply["sys_id"]["value"], reply["number"]["value"]
        path = f"/api/sn_chg_rest/change/standard/{change_id}"
        self.call("PATCH", path, {"state": SCHEDULED})
        self.call("PATCH", path, {"state": IMPLEMENT, "work_start": servicenow_time(started)})
        return change_id, number

    def close(self, change_id, code, notes, ended):
        path = f"/api/sn_chg_rest/change/standard/{change_id}"
        self.call("PATCH", path, {"state": REVIEW, "work_end": servicenow_time(ended)})
        self.call("PATCH", path, {"state": CLOSED, "close_code": code, "close_notes": notes})


# Slack and Grafana.

def slack(token, method, **fields):
    request = urllib.request.Request(f"https://slack.com/api/{method}", data=json.dumps(fields).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        reply = json.loads(response.read())
    if not reply.get("ok"):
        print(f"Slack {method}: {reply.get('error')}")
    return reply


def annotate(secrets, text, at):
    request = urllib.request.Request(
        secrets["grafana/stack-url"].rstrip("/") + "/api/annotations", method="POST",
        data=json.dumps({"time": int(at.timestamp() * 1000), "text": text,
                         "tags": ["release-demo", "flag", "orders-service"]}).encode(),
        headers={"Authorization": f"Bearer {secrets['grafana/service-account-token']}",
                 "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except Exception as error:  # an annotation never stops the record
        print(f"Grafana annotation not sent: {error.__class__.__name__}")


# The three events.

def head(name, state, mention, settings):
    return f"*Flag change: {name} {state}* · {settings['service']} {settings['environment']} · requested by {mention}"


def watch_minutes(deployment, settings):
    """The deployment's bake time: 10 minutes for /flag, none for reset.py's."""
    return int(deployment.get("FinalBakeTimeInMinutes", settings["watch_minutes"]))


def deployment_line(deployment, change, settings):
    minutes = watch_minutes(deployment, settings)
    watching = f" · watching {settings['alarm']} for {minutes} minutes" if minutes else ""
    return f"{change} · AWS AppConfig deployment {deployment['DeploymentNumber']}{watching}"


def start(deployment, tags, settings, secrets, sn, arn):
    if tags.get("change_number"):
        return tags  # recorded already: a retried event
    name, state = switched(deployment, tags)
    user, mention, display = requester(deployment, tags, settings["people"])
    started = deployment["StartedAt"]
    number = deployment["DeploymentNumber"]
    short = f"Flag {name} {state} in {settings['service']} {settings['environment']}"
    description = (f"AWS AppConfig deployment {number} of configuration version {deployment['ConfigurationVersion']} "
                   f"of the flags profile to {settings['environment']}, with the strategy "
                   f"{deployment.get('DeploymentStrategyId', '')}: {name} {state}. Requested by {display}"
                   f"{' in Slack' if 'Slack' in (deployment.get('Description') or '') else ''}."
                   + (f" AWS AppConfig watches the alarm {settings['alarm']} for {watch_minutes(deployment, settings)} "
                      "minutes and rolls the flags back if it goes into ALARM." if watch_minutes(deployment, settings)
                      else " No bake time: it completes at once."))
    change_id, change = sn.open(settings, {"short_description": short, "description": description,
                                           "correlation_id": f"appconfig:{arn.split(':application/', 1)[1]}"},
                                user, started, watch_minutes(deployment, settings) or 1)
    text = f"{head(name, state, mention, settings)}\n\n{deployment_line(deployment, change, settings)}"
    posted = slack(secrets["slack/bot-token"], "chat.postMessage", channel=secrets["slack/channel-id"], text=text,
                   unfurl_links=False)
    annotate(secrets, f"flag {name} {state} ({display})", started)
    kept = {"change_sys_id": change_id, "change_number": change, "slack_channel": posted.get("channel", ""),
            "slack_ts": posted.get("ts", ""), "slack_text": "", "flag": name, "state": state}
    aws("appconfig").tag_resource(ResourceArn=arn, Tags={k: v for k, v in kept.items() if k != "slack_text"})
    print(f"start: {change} for deployment {number}, {name} {state}")
    return {**tags, **kept, "slack_text": text}


def message_text(deployment, tags, settings):
    """The Slack message as start posted it, to add a line to."""
    name, state = tags.get("flag"), tags.get("state")
    _, mention, _ = requester(deployment, tags, settings["people"])
    return f"{head(name, state, mention, settings)}\n\n{deployment_line(deployment, tags['change_number'], settings)}"


def add_line(tags, secrets, text, line):
    if tags.get("slack_ts"):
        slack(secrets["slack/bot-token"], "chat.update", channel=tags["slack_channel"], ts=tags["slack_ts"],
              text=f"{text}\n\n{line}")


def complete(deployment, tags, settings, secrets, sn, arn):
    if tags.get("recorded") in ("complete", "rolled-back"):
        return
    name, state = tags["flag"], tags["state"]
    ended = deployment.get("CompletedAt") or datetime.datetime.now(datetime.timezone.utc)
    minutes = watch_minutes(deployment, settings)
    line = f"Done: {name} is {state} in {settings['environment']}." + (f" No alarm in {minutes} minutes." if minutes else "")
    sn.close(tags["change_sys_id"], "successful",
             f"AWS AppConfig deployment {deployment['DeploymentNumber']} completed at {clock(ended)}: {line}", ended)
    add_line(tags, secrets, tags.get("slack_text") or message_text(deployment, tags, settings), line)
    annotate(secrets, f"flag {name} {state}: done" + (f", no alarm in {minutes} minutes" if minutes else ""), ended)
    aws("appconfig").tag_resource(ResourceArn=arn, Tags={"recorded": "complete"})
    print(f"complete: {tags['change_number']} closed successful")


def rolled_back(deployment, tags, settings, secrets, sn, arn):
    if tags.get("recorded") in ("complete", "rolled-back"):
        return
    name, state = tags["flag"], tags["state"]
    back = "off" if state == "on" else "on"
    at, cause = event_time(deployment, "ROLLBACK_STARTED", "ROLLBACK_COMPLETED")
    at = at or datetime.datetime.now(datetime.timezone.utc)
    done, _ = event_time(deployment, "ROLLBACK_COMPLETED")
    done = done or at
    fired = alarm_time(settings["alarm"], at, deployment["StartedAt"]) if cause == "CLOUDWATCH_ALARM" else None
    if fired:
        seconds = math.floor((at - fired).total_seconds())
        why = (f"AWS AppConfig rolled it back at {clock(at)}, {seconds}s after alarm {settings['alarm']} "
               f"went into ALARM at {clock(fired)}.")
        short = f"Flag {name} {back} in {settings['service']} {settings['environment']}, by AWS AppConfig on alarm"
        line_start = f"Rolled back by alarm {settings['alarm']} at {clock(at)}, {seconds}s after it fired."
        annotation = f"flag {name} {back}: rolled back by alarm"
    else:
        why = f"The deployment was stopped at {clock(at)}, before its bake time ended: AWS AppConfig put the flags back."
        short = (f"Flag {name} {back} in {settings['service']} {settings['environment']}, "
                 f"deployment {deployment['DeploymentNumber']} stopped")
        line_start = f"Stopped at {clock(at)}, before the bake time ended."
        annotation = f"flag {name} {back}: deployment stopped"
    sn.close(tags["change_sys_id"], "unsuccessful", why, at)
    second_id, second = sn.open(settings, {
        "short_description": short,
        "description": f"{why} AWS AppConfig deployment {deployment['DeploymentNumber']} ({tags['change_number']}) "
                       f"switched {name} {state}; the rollback put the configuration before it back: {name} {back}.",
        "correlation_id": f"appconfig:{arn.split(':application/', 1)[1]}:rollback"}, PIPELINE, at, 1)
    sn.close(second_id, "successful", f"{why} {name} is {back} again in {settings['environment']}.", done)
    add_line(tags, secrets, tags.get("slack_text") or message_text(deployment, tags, settings),
             f"{line_start} {name} is {back} again. {second}")
    annotate(secrets, annotation, at)
    aws("appconfig").tag_resource(ResourceArn=arn, Tags={"recorded": "rolled-back", "rollback_change": second})
    print(f"rolled back: {tags['change_number']} closed unsuccessful, {second} for the switch back")


def handler(event, context):
    detail = event.get("detail") or {}
    kind = detail.get("Type", "")
    settings = config()
    if detail.get("Environment", {}).get("Name") != settings["environment"]:
        print(f"{kind}: not {settings['environment']}, not recorded")
        return {"recorded": False}
    application, environment_id = detail["Application"]["Id"], detail["Environment"]["Id"]
    number = int(detail["DeploymentNumber"])
    region, account = context.invoked_function_arn.split(":")[3:5]
    arn = (f"arn:aws:appconfig:{region}:{account}:application/{application}/environment/{environment_id}"
           f"/deployment/{number}")
    deployment = aws("appconfig").get_deployment(ApplicationId=application, EnvironmentId=environment_id,
                                                 DeploymentNumber=number)
    deployment["ApplicationId"] = application
    tags = aws("appconfig").list_tags_for_resource(ResourceArn=arn).get("Tags", {})
    secrets = parameters()
    sn = ServiceNow(secrets["servicenow/instance-url"], secrets["servicenow/release-pipeline-password"])
    tags = start(deployment, tags, settings, secrets, sn, arn)  # every event: a lost start is recorded late
    if kind == "OnDeploymentComplete":
        complete(deployment, tags, settings, secrets, sn, arn)
    elif kind == "OnDeploymentRolledBack":
        rolled_back(deployment, tags, settings, secrets, sn, arn)
    return {"recorded": True, "kind": kind, "deployment": number}
