"""Grafana Cloud for demo 4: log lines to Loki and annotations on the dashboard.

The load generator pushes one line per request, so Grafana shows each
version's share, error rate and latency within seconds. The canary marks its
window and its alarm as region annotations. Grafana only shows: CloudWatch's
alarms make the decision. Nothing sent here can stop a release: a failed
push prints a warning and the run goes on.
"""

import base64
import json
import sys
import time
import urllib.request

import pricing  # noqa: F401  (puts scripts/ on the path)
from common import get_parameter, ssm_client

USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


def _send(method, url, body, headers):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method=method, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except Exception as error:  # never stops a release
        print(f"Grafana {url.split('/')[2]}: not sent ({error})", file=sys.stderr)
        return None


class Loki:
    """Grafana Cloud Logs, through the Loki push API."""

    def __init__(self, ssm=None):
        ssm = ssm or ssm_client()
        self.url = get_parameter(ssm, "grafana/logs-url").rstrip("/") + "/loki/api/v1/push"
        user, token = get_parameter(ssm, "grafana/logs-user"), get_parameter(ssm, "grafana/logs-token")
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()

    def push(self, streams):
        """streams: {labels (a tuple of pairs): [(time_ns, dict), ...]}. True when Loki took them."""
        body = {"streams": [{"stream": dict(labels), "values": [[str(ns), json.dumps(line)] for ns, line in lines]}
                            for labels, lines in streams.items() if lines]}
        if not body["streams"]:
            return True
        return _send("POST", self.url, body, {"Authorization": self.auth}) is not None


class Annotations:
    """Region annotations on the stack, found by the dashboard through their tags."""

    def __init__(self, ssm=None):
        ssm = ssm or ssm_client()
        self.base = get_parameter(ssm, "grafana/stack-url").rstrip("/") + "/api/annotations"
        self.auth = "Bearer " + get_parameter(ssm, "grafana/service-account-token")

    def start(self, text, tags, at=None, until=None):
        """A region from at (default now) to until, the planned end, in seconds.
        Grafana draws the part up to now; end() sets the real end. Returns its id."""
        at = at or time.time()
        reply = _send("POST", self.base, {"time": int(at * 1000), "timeEnd": int((until or at) * 1000), "text": text,
                                          "tags": ["release-demo", pricing.SERVICE, *tags]},
                      {"Authorization": self.auth})
        return reply.get("id") if reply else None

    def end(self, annotation_id, text=None, at=None):
        if not annotation_id:
            return
        body = {"timeEnd": int((at or time.time()) * 1000)}
        if text:
            body["text"] = text
        _send("PATCH", f"{self.base}/{annotation_id}", body, {"Authorization": self.auth})


def connect():
    """Loki and annotations, or (None, None) when Grafana isn't set up: the canary runs without it."""
    try:
        ssm = ssm_client()
        return Loki(ssm), Annotations(ssm)
    except SystemExit:
        print("Grafana isn't set up (see scripts/secrets.py): the canary runs without its dashboard.", file=sys.stderr)
        return None, None
