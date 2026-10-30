"""Where a rotation leaves its record, through the helpers the pipeline uses:
Slack (the message that leaked, and a reply in its thread), a ServiceNow
incident through the Table API as release.pipeline, one Splunk event and
one Grafana annotation tagged secret-rotation, which the Releases
dashboard's annotation queries leave out."""

import json
import re
import urllib.parse
import urllib.request

# A JFrog access token as it would sit in a message: a JWT, three base64url parts.
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
INCIDENT_PREFIX = "JFrog token of release-pipeline exposed: "
CMDB_ITEM = "jfrog-container-registry"
RESOLVED = "6"
# The instance's own choice for a solved incident, by label, best first.
SOLVED = ("Solution provided", "Solved (Permanently)", "Solved Remotely (Permanently)", "Solved (Work Around)")


# Slack

def slack_get(method, token, **query):
    request = urllib.request.Request(f"https://slack.com/api/{method}?{urllib.parse.urlencode(query)}",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        reply = json.loads(response.read())
    if not reply.get("ok"):
        raise RuntimeError(f"Slack {method}: {reply.get('error')}")
    return reply


def history(token, channel, limit=100):
    """The channel's last messages, newest first. Needs channels:history (groups:history if private)."""
    return slack_get("conversations.history", token, channel=channel, limit=limit).get("messages", [])


def tokens_in(text):
    return JWT.findall(text or "")


def reply_in_thread(token, channel, ts, text):
    from notify import slack

    return slack("chat.postMessage", token, channel=channel, thread_ts=ts, text=text, unfurl_links=False)


def author(user_id, everyone):
    """The display name people.yaml gives a Slack member id, as the incident names Sam."""
    for person in everyone.values():
        if user_id and person.get("slack_user_id") == user_id:
            return person["display_name"]
    return "someone in Slack"


# ServiceNow

def solved_code(sn):
    """The close code a resolved incident takes, from the instance's own choices."""
    rows = sn.call("GET", "/api/now/table/sys_choice", sysparm_query="name=incident^element=close_code^inactive=false",
                   sysparm_fields="value,label") or []
    labels = {row["label"]: row["value"] for row in rows}
    for wanted in SOLVED:
        if wanted in labels:
            return labels[wanted]
    solved = [row["value"] for row in rows if row["label"].lower().startswith(("solved", "solution"))]
    return solved[0] if solved else "Solution provided"


def open_incident(sn, reason, description):
    item = sn.find("cmdb_ci_appl", f"name={CMDB_ITEM}", "sys_id")
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")
    caller = sn.find("sys_user", "user_name=release.pipeline", "sys_id")
    fields = {"short_description": INCIDENT_PREFIX + reason, "description": description, "category": "software",
              "impact": "2", "urgency": "1", "caller_id": caller["sys_id"] if caller else "",
              "assignment_group": group["sys_id"] if group else ""}
    if item:
        fields["cmdb_ci"] = item["sys_id"]
    created = sn.call("POST", "/api/now/table/incident", fields)
    return {"sys_id": created["sys_id"], "number": created["number"],
            "url": f"{sn.url}/incident.do?sys_id={created['sys_id']}"}


def note(sn, incident, text):
    sn.call("PATCH", f"/api/now/table/incident/{incident['sys_id']}", {"work_notes": text})


def resolve(sn, incident, notes):
    sn.call("PATCH", f"/api/now/table/incident/{incident['sys_id']}",
            {"state": RESOLVED, "close_code": solved_code(sn), "close_notes": notes})


def open_leak_incidents(sn):
    return sn.call("GET", "/api/now/table/incident", sysparm_fields="sys_id,number,short_description,state",
                   sysparm_query=f"active=true^short_descriptionSTARTSWITH{INCIDENT_PREFIX}^ORDERBYnumber") or []


# Splunk and Grafana

def event(ssm, body):
    from events import to_splunk

    return to_splunk(ssm, {"service": CMDB_ITEM, "stage": "Secret rotation", "environment": None, **body})


def annotate(ssm, text):
    from events import annotate as grafana

    return grafana(ssm, text, ["secret-rotation"])
