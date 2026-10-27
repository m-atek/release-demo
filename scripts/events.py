"""Send one release event to Splunk and Grafana, or a Grafana annotation.

    python scripts/events.py --stage Build --result ok --seconds 12.4
    python scripts/events.py --stage Production --result ok --environment production --deploy
    python scripts/events.py annotate "orders-service 1.4.0 released" --tags release

Each event carries the build, service, version, environment, change number,
Jira key, stage, result and duration. A production deploy also carries the
commit time, so Grafana can compute the lead time, and after a rollback the
recovery time. Splunk gets it through its HTTP Event Collector, Grafana Cloud
Logs through the Loki push API, one JSON line each.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.request

import run_state
from common import get_parameter, ssm_client

USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


def send(url, body, headers):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status
    except Exception as error:  # an event that can't be sent never stops a release
        print(f"event to {url.split('/')[2]} not sent: {error}", file=sys.stderr)
        return 0


def event(args, state):
    body = {
        "build": state["build"]["number"], "build_url": state["build"].get("url"),
        "service": state["service"], "version": state["version"], "environment": args.environment,
        "change": state.get("change", {}).get("number"), "jira_key": state["jira_key"],
        "stage": args.stage, "result": args.result, "duration_seconds": args.seconds,
        "requested_by": state.get("requested_by"), "option": state.get("option"),
    }
    if args.deploy:
        body["deploy"] = True
        body["commit_time"] = state.get("commit_time")
        body["lead_time_seconds"] = round(time.time() - state["commit_time"]) if state.get("commit_time") else None
    if args.recovery_seconds is not None:
        body["recovery_seconds"] = args.recovery_seconds
    return body


def to_splunk(ssm, body):
    url = os.environ.get("SPLUNK_HEC_URL", "http://localhost:8088") + "/services/collector/event"
    token = get_parameter(ssm, "splunk/hec-token")
    return send(url, {"time": time.time(), "sourcetype": "release:event", "source": "release-demo", "event": body},
                {"Authorization": f"Splunk {token}"})


def to_loki(ssm, body):
    url = get_parameter(ssm, "grafana/logs-url").rstrip("/") + "/loki/api/v1/push"
    user, token = get_parameter(ssm, "grafana/logs-user"), get_parameter(ssm, "grafana/logs-token")
    labels = {"source": "release-demo", "service": body["service"], "environment": body["environment"] or "pipeline"}
    stream = {"stream": labels, "values": [[str(time.time_ns()), json.dumps(body)]]}
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    return send(url, {"streams": [stream]}, {"Authorization": f"Basic {auth}"})


def annotate(ssm, text, tags):
    url = get_parameter(ssm, "grafana/stack-url").rstrip("/") + "/api/annotations"
    token = get_parameter(ssm, "grafana/service-account-token")
    return send(url, {"time": int(time.time() * 1000), "text": text, "tags": ["release-demo", *tags]},
                {"Authorization": f"Bearer {token}"})


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "annotate":
        parser = argparse.ArgumentParser()
        parser.add_argument("command")
        parser.add_argument("text")
        parser.add_argument("--tags", default="")
        args = parser.parse_args()
        status = annotate(ssm_client(), args.text, [t for t in args.tags.split(",") if t])
        print(f"Grafana annotation: {status}")
        return

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--result", required=True, choices=["ok", "failed", "skipped", "rolled-back"])
    parser.add_argument("--seconds", type=float, default=None)
    parser.add_argument("--environment", default=None)
    parser.add_argument("--deploy", action="store_true", help="a production deploy: adds the commit time")
    parser.add_argument("--recovery-seconds", type=float, default=None)
    args = parser.parse_args()

    ssm = ssm_client()
    body = event(args, run_state.load())
    print(f"event {args.stage} {args.result}: Splunk {to_splunk(ssm, body)}, Grafana {to_loki(ssm, body)}")


if __name__ == "__main__":
    sys.exit(main())
