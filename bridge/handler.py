"""The bridge: one Lambda function behind a function URL, for calls from
outside the tools host that change the platform's records.

    POST /events    an event from one of the platform's own machines,
                    recorded in ServiceNow, Slack and Grafana (bridge/events.py).
                    Demo 19's drift recorder sends kind "drift".

Demo 14 adds its approval routes here (GitHub's deployment protection rule
and ServiceNow's approvals), demo 21 AWS AppConfig's events. Each route checks
its own caller, so the URL needs no AWS auth. Standard library and boto3
only, both in Lambda's Python runtime; infra/approval_bridge.tf packages
this folder.
"""

import json

import events

ROUTES = {("POST", "/events"): events.handle}


def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    route = ROUTES.get((http.get("method", ""), event.get("rawPath", "")))
    if route is None:
        return response(404, {"error": "no such route"})
    return route(event)


def response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}
