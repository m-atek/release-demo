"""Send one release event to Splunk and Grafana, or a Grafana annotation.

    python scripts/events.py --stage Build --result ok --seconds 12.4
    python scripts/events.py --stage Production --result ok --environment production --deploy
    python scripts/events.py annotate "orders-service 1.4.0 released" --tags release
    python scripts/events.py tests --job release      one event per test, and one for the run (demo 15)

Each event carries the build, service, version, environment, change number,
Jira key, stage, result and duration. A production deploy also carries the
commit time, so Grafana can compute the lead time, and after a rollback the
recovery time. Splunk gets it through its HTTP Event Collector, Grafana Cloud
Logs through the Loki push API, one JSON line each.

tests reads the run's JUnit files (.release/reports/junit.xml, and
junit-attempts.xml after a rerun) and sends one event per test: service,
version, job (release or tests), build, run URL, commit, test (pytest node
id), outcome, attempts and seconds; then one summary of the run: tests, flaky,
failed twice, the Build stage's seconds, Test's first pass in seconds, the
rerun's seconds and the seconds saved. In Loki they share one stream with the
labels source, service and kind=test; the test's name stays in the JSON line.
"""

import argparse
import base64
import datetime
import json
import os
import sys
import time
import urllib.request

import junit_results
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
    if getattr(args, "job", None):
        body["job"] = args.job  # demo 15's orders-service-tests, which the release views leave out
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


def test_events(state, job, results=None):
    """The per-test events and the run's summary, from the JUnit files."""
    results = results or junit_results.read(run_state.REPORTS)
    common = {"service": state["service"], "version": state["version"], "job": job,
              "build": state["build"]["number"], "build_url": state["build"].get("url"), "commit": state.get("commit")}
    events = []
    for test in results["tests"]:
        event = {"event": "test", **common, "test": test["test"], "name": junit_results.short(test["test"]),
                 "outcome": test["outcome"], "attempts": test["attempts"], "seconds": test["seconds"],
                 # 1 or 0, so a dashboard sums them per test.
                 "flaky": int(test["outcome"] == "flaky"),
                 "failed_twice": int(test["outcome"] == "failed" and test["attempts"] == 2)}
        if test["quarantine"]:
            event.update(quarantine=test["quarantine"], quarantine_until=test["quarantine_until"],
                         quarantine_until_ms=until_ms(test["quarantine_until"]))
        events.append(event)
    build_seconds = float((state.get("stages", {}).get("Build") or {}).get("seconds") or 0)
    events.append({"event": "summary", **common, **junit_results.summary(results, build_seconds)})
    return events


def until_ms(day):
    """Midday of a quarantine's last day, as Grafana's dates want it: milliseconds since the epoch."""
    try:
        end = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(12), datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return int(end.timestamp() * 1000)


def send_tests(ssm, events):
    """All of a run's test events in one request each: Splunk's collector takes them
    one after the other, Loki as one stream with a nanosecond apart."""
    url = os.environ.get("SPLUNK_HEC_URL", "http://localhost:8088") + "/services/collector/event"
    token = get_parameter(ssm, "splunk/hec-token")
    now = time.time()
    body = "".join(json.dumps({"time": now, "sourcetype": "release:test", "source": "release-demo", "event": event})
                   for event in events)
    request = urllib.request.Request(url, data=body.encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT, "Authorization": f"Splunk {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            splunk = response.status
    except Exception as error:  # an event that can't be sent never stops a release
        print(f"test events to Splunk not sent: {error}", file=sys.stderr)
        splunk = 0
    loki_url = get_parameter(ssm, "grafana/logs-url").rstrip("/") + "/loki/api/v1/push"
    user, token = get_parameter(ssm, "grafana/logs-user"), get_parameter(ssm, "grafana/logs-token")
    labels = {"source": "release-demo", "service": events[0]["service"], "kind": "test"}
    start = time.time_ns()
    values = [[str(start + index), json.dumps(event)] for index, event in enumerate(events)]
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    loki = send(loki_url, {"streams": [{"stream": labels, "values": values}]}, {"Authorization": f"Basic {auth}"})
    return splunk, loki


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "tests":
        parser = argparse.ArgumentParser()
        parser.add_argument("command")
        parser.add_argument("--job", default="release", choices=["release", "tests"])
        args = parser.parse_args()
        events = test_events(run_state.load(), args.job)
        if len(events) == 1:
            print("No test results to send: the stage ended before any test ran.")
            return
        splunk, loki = send_tests(ssm_client(), events)
        summary = events[-1]
        print(f"test events: {summary['tests']} tests, {summary['flaky']} flaky, {summary['failed_twice']} failed twice: "
              f"Splunk {splunk}, Grafana {loki}")
        return
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
    parser.add_argument("--job", default=None, help="a job other than the release, such as tests")
    args = parser.parse_args()

    ssm = ssm_client()
    body = event(args, run_state.load())
    print(f"event {args.stage} {args.result}: Splunk {to_splunk(ssm, body)}, Grafana {to_loki(ssm, body)}")


if __name__ == "__main__":
    sys.exit(main())
