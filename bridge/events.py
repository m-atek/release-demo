"""The event bridge: POST /events, an event from one of the platform's own
machines, written to the records a change board reads.

The caller sends a bearer token, bridge/events-token in SSM, and JSON with a
kind. Kind "drift" comes from demo 19's drift recorder on the cluster host
when Argo CD has put back a change made to production by hand. For it the
bridge writes, with every time in Europe/Amsterdam:

- an incident, through ServiceNow's Table API as release.pipeline, naming the
  CI, the diff, who made the edit and the three times, with one work note
  that holds the timeline;
- a new message in #releases with the same facts and the incident's number;
- a Grafana annotation tagged drift, and one line to Grafana Cloud Logs.
  Not Splunk: its collector listens on the tools host only.

The event's id becomes the incident's correlation id, so a retried event
finds its incident instead of opening a second. The function reads only the
parameters it writes with, and never logs a value.
"""

import base64
import datetime
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

SSM_PREFIX = "/release-demo/"
CACHE_SECONDS = 5 * 60
USER_AGENT = "release-demo-bridge/1.0 (+https://github.com/m-atek/release-demo)"
PARAMETERS = ["bridge/events-token", "servicenow/instance-url", "servicenow/release-pipeline-password",
              "slack/bot-token", "slack/channel-id", "grafana/stack-url", "grafana/service-account-token",
              "grafana/logs-url", "grafana/logs-user", "grafana/logs-token"]
SHORT_DESCRIPTION = "{service} in production changed outside Git; Argo CD put it back"
KEEP = "To keep a change, open a pull request to {path}."
IN_PROGRESS = "2"
DRIFT_PREFIX = "argocd-drift:"


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def handle(event):
    try:
        values = parameters(*PARAMETERS)
        authorize(event, values["bridge/events-token"])
        body = json.loads(raw_body(event) or "{}")
        if body.get("kind") != "drift":
            raise Refused(400, f"unknown kind {body.get('kind')!r}: this bridge records drift")
        result = drift(body, values)
        return reply(200, result)
    except Refused as refusal:
        log(f"refused ({refusal.status}): {refusal}")
        return reply(refusal.status, {"error": str(refusal)})
    except Exception as error:  # the log has the detail; the caller retries
        log(f"failed: {error.__class__.__name__}: {error}")
        return reply(502, {"error": f"not recorded ({error.__class__.__name__}); the bridge's log says why"})


def raw_body(event):
    body = event.get("body") or ""
    return base64.b64decode(body).decode() if event.get("isBase64Encoded") else body


def authorize(event, token):
    headers = {name.lower(): value for name, value in (event.get("headers") or {}).items()}
    sent = headers.get("authorization", "")
    if not token:
        raise Refused(503, "bridge/events-token isn't set in SSM")
    if not sent.startswith("Bearer ") or not hmac.compare_digest(sent[len("Bearer "):].encode(), token.encode()):
        raise Refused(401, "no valid bearer token")


# Times, in the zone the people reading the records work in.

def parse_time(text):
    """An RFC 3339 time, with or without fractions, as an aware UTC datetime."""
    text = text.strip().replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        digits = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:digits][:6].ljust(6, '0')}{rest[digits:]}"
    return datetime.datetime.fromisoformat(text).astimezone(datetime.timezone.utc)


def last_sunday(year, month):
    """The last Sunday of March or October."""
    day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


def amsterdam(moment):
    """Central European Time: UTC+2 from the last Sunday of March, 01:00 UTC,
    to the last Sunday of October, 01:00 UTC, else UTC+1. No time zone
    database needed in the function."""
    moment = moment.astimezone(datetime.timezone.utc)
    start = datetime.datetime.combine(last_sunday(moment.year, 3), datetime.time(1), datetime.timezone.utc)
    end = datetime.datetime.combine(last_sunday(moment.year, 10), datetime.time(1), datetime.timezone.utc)
    hours = 2 if start <= moment < end else 1
    return moment.astimezone(datetime.timezone(datetime.timedelta(hours=hours)))


def clock(text):
    return amsterdam(parse_time(text)).strftime("%H:%M:%S")


def day_and_zone(text):
    local = amsterdam(parse_time(text))
    return f"{local.day} {local.strftime('%B %Y')}, Europe/Amsterdam"


# What the records say.

def tool(edit):
    """kubectl's field manager in words: kubectl-scale is "kubectl scale"."""
    manager = edit.get("field_manager") or ""
    if manager == "kubectl-client-side-apply":
        return "kubectl apply"
    if manager.startswith("kubectl-"):
        return "kubectl " + manager[len("kubectl-"):]
    return manager or edit.get("user_agent", "").split(" ")[0] or "an API call"


def groups_text(edit):
    groups = [g for g in edit.get("groups", []) if not g.startswith("system:")]
    return f" (group {', '.join(groups)})" if groups else ""


def live_text(event):
    return ", ".join(f"{d['path']} {d['live']}" for d in event["diff"])


def git_text(event):
    return ", ".join(str(d["git"]) for d in event["diff"])


def first_edit(event):
    return min(event["edits"], key=lambda e: parse_time(e["at"]))


def setting(event, edit):
    """What one edit set: "spec.replicas set to 5", from the paths it touched."""
    paths = {d["path"] for d in event["diff"]}
    changed = [f"{path} set to {value}" for path, value in flatten(edit.get("request") or {}) if path in paths]
    return ", ".join(changed) or f"{edit.get('verb', 'changed')} {edit.get('resource', event['resource'])}"


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, value


def timeline(event):
    lines = []
    for edit in sorted(event["edits"], key=lambda e: parse_time(e["at"])):
        lines.append(f"{clock(edit['at'])} {setting(event, edit)} by {edit['user']}{groups_text(edit)}, {tool(edit)}")
    if event.get("out_of_sync_at"):
        lines.append(f"{clock(event['out_of_sync_at'])} Argo CD: OutOfSync")
    put_back = ", ".join(f"{d['path']} {d['git']}" for d in event["diff"])
    lines.append(f"{clock(event['reverted_at'])} Argo CD self-heal: {put_back}, from Git at {event['revision'][:7]}")
    certificate = event.get("certificate")
    if certificate:
        lines.append(f"Break-glass certificate for {certificate['user']} issued {clock(certificate['issued'])}, "
                     f"valid until {clock(certificate['expires'])}")
    lines.append(KEEP.format(path=event["path"]))
    return "\n".join(lines)


def description(event):
    edit = first_edit(event)
    changes = "; ".join(f"{d['path']} {d['live']}, Git {d['git']}" for d in event["diff"])
    lines = [
        f"{event['service']} in production on the cluster host ({event['resource']} in {event['namespace']}) was "
        "changed outside Git, and Argo CD put Git's values back.",
        "",
        f"Changed: {changes}.",
        f"Edited by {edit['user']}{groups_text(edit)} with {tool(edit)}.",
        "",
        f"Times ({day_and_zone(edit['at'])}):",
        f"Hand edit: {clock(edit['at'])}",
    ]
    if event.get("out_of_sync_at"):
        lines.append(f"Argo CD found it OutOfSync: {clock(event['out_of_sync_at'])}")
    lines += [f"Argo CD put Git's values back: {clock(event['reverted_at'])}, after {event['seconds']} s, "
              f"from Git at {event['revision'][:7]}.", "", KEEP.format(path=event["path"])]
    return "\n".join(lines)


def slack_text(event, number):
    edit = first_edit(event)
    who = f"{edit['user']}{' (break-glass)' if event.get('break_glass') else ''}"
    return "\n".join([
        f"*Production drifted from Git* · {event['service']} · {number}",
        f"{live_text(event)}, set by {who} at {clock(edit['at'])}.",
        f"Argo CD put Git's {git_text(event)} back at {clock(event['reverted_at'])}, after {event['seconds']}s.",
        KEEP.format(path=event["path"]),
    ])


def check_event(event):
    missing = [key for key in ("id", "service", "namespace", "resource", "path", "cmdb_item", "revision", "edits",
                               "diff", "reverted_at", "seconds") if key not in event]
    if missing or not event.get("edits") or not event.get("diff"):
        raise Refused(400, f"a drift event needs {', '.join(missing) or 'edits and a diff'}")


# Writing them.

def drift(event, values):
    check_event(event)
    sn = ServiceNow(values["servicenow/instance-url"], "release.pipeline", values["servicenow/release-pipeline-password"])
    correlation = DRIFT_PREFIX + event["id"]
    existing = sn.find("incident", f"correlation_id={correlation}", "sys_id,number")
    if existing:
        log(f"{event['id']}: already recorded as {existing['number']}")
        return {"incident": existing["number"], "duplicate": True}
    ci = sn.find("cmdb_ci", f"name={event['cmdb_item']}", "sys_id")
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")
    caller = sn.find("sys_user", "user_name=release.pipeline", "sys_id")
    fields = {
        "short_description": SHORT_DESCRIPTION.format(service=event["service"]),
        "description": description(event),
        "category": "software", "impact": "3", "urgency": "3", "state": IN_PROGRESS,
        "cmdb_ci": ci["sys_id"] if ci else "", "assignment_group": group["sys_id"] if group else "",
        "caller_id": caller["sys_id"] if caller else "", "correlation_id": correlation,
        "work_notes": timeline(event),
    }
    incident = sn.call("POST", "/api/now/table/incident", fields)
    number = incident["number"]
    log(f"{event['id']}: {number}")

    posted = slack(values["slack/bot-token"], "chat.postMessage", channel=values["slack/channel-id"],
                   text=slack_text(event, number), unfurl_links=False)
    annotation = send(values["grafana/stack-url"].rstrip("/") + "/api/annotations",
                      {"time": int(parse_time(event["reverted_at"]).timestamp() * 1000),
                       "text": f"{event['service']} in production: hand edit put back by Argo CD",
                       "tags": ["release-demo", "drift", event["service"]]},
                      {"Authorization": f"Bearer {values['grafana/service-account-token']}"})
    line = {"kind": "drift", "service": event["service"], "environment": "production", "incident": number,
            "edited_by": first_edit(event)["user"], "diff": event["diff"], "seconds": event["seconds"],
            "revision": event["revision"]}
    auth = base64.b64encode(f"{values['grafana/logs-user']}:{values['grafana/logs-token']}".encode()).decode()
    logs = send(values["grafana/logs-url"].rstrip("/") + "/loki/api/v1/push",
                {"streams": [{"stream": {"source": "release-demo", "service": event["service"],
                                         "environment": "production", "kind": "drift"},
                              "values": [[str(time.time_ns()), json.dumps(line)]]}]},
                {"Authorization": f"Basic {auth}"})
    return {"incident": number, "sys_id": incident["sys_id"], "slack": bool(posted.get("ok")),
            "annotation": annotation, "logs": logs}


class ServiceNow:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method, path, body=None, **query):
        query = {k: v for k, v in query.items() if v is not None}
        request = urllib.request.Request(
            f"{self.url}{path}{'?' + urllib.parse.urlencode(query) if query else ''}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode()
        if "hibernat" in raw.lower():
            raise RuntimeError("ServiceNow is asleep: wake it at developer.servicenow.com")
        return json.loads(raw)["result"] if raw else None

    def find(self, table, query, fields=None):
        rows = self.call("GET", f"/api/now/table/{table}", sysparm_query=query, sysparm_limit=1, sysparm_fields=fields)
        return rows[0] if rows else None


def slack(token, method, **fields):
    request = urllib.request.Request(f"https://slack.com/api/{method}", data=json.dumps(fields).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            reply = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
        log(f"Slack {method}: {error.__class__.__name__}")
        return {"ok": False}
    if not reply.get("ok"):
        log(f"Slack {method}: {reply.get('error')}")
    return reply


def send(url, body, headers):
    """POST JSON; the status, or 0. Grafana missing an event never fails the record."""
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except Exception as error:
        log(f"POST {url.split('/')[2]}: {error.__class__.__name__}")
        return 0


_ssm, _parameters = [], {}


def parameters(*names):
    """SSM values by name, None for a missing or unset one, kept for five minutes."""
    if not _ssm:
        _ssm.append(boto3.client("ssm"))
    now = time.time()
    stale = [name for name in names if name not in _parameters or _parameters[name][1] < now]
    for start in range(0, len(stale), 10):
        reply = _ssm[0].get_parameters(Names=[SSM_PREFIX + n for n in stale[start:start + 10]], WithDecryption=True)
        for parameter in reply["Parameters"]:
            value = parameter["Value"]
            _parameters[parameter["Name"].removeprefix(SSM_PREFIX)] = (None if value == "unset" else value,
                                                                       now + CACHE_SECONDS)
    return {name: _parameters.get(name, (None, 0))[0] for name in names}


def reply(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def log(message):
    print(message)
