"""What video 27's tests share: the run state in a temporary folder, and fakes for
CloudWatch and Jira. Nothing here leaves the machine.

    .venv/bin/python -m pytest -q -p no:cacheprovider scripts/demos/error_budget/tests
"""

import datetime
import os
import re
import sys
from pathlib import Path

import pytest

# No test can reach a real account, whatever it ends up calling: no profile, no credentials, no instance role.
for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(name, None)
os.environ.update(AWS_PROFILE="no-such-profile-in-tests", AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                  AWS_EC2_METADATA_DISABLED="true")

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

import run_state  # noqa: E402

# Wednesday 4 November 2026, 13:47:20 in Amsterdam (CET, UTC+1).
NOW = datetime.datetime(2026, 11, 4, 12, 47, 20, tzinfo=datetime.timezone.utc).timestamp()
SAM = "U0SAM000001"
BUDGET_SUMMARY = "Error budget spent: orders-service, 99.9% over 24 hours"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


def window(as_of, per_minute=60, extra=0, failing=None, hours=24):
    """[(minute, requests, failed)] for a window ending at as_of. failing is (first, last) in
    minutes ago, 1 being the newest minute in the window, with 3 failures in each of those."""
    minutes = range(as_of - hours * 3600, as_of, 60)
    rows = []
    for index, minute in enumerate(minutes):
        ago = len(minutes) - index
        failed = 3 if failing and failing[1] <= ago <= failing[0] else 0
        rows.append((minute, per_minute + (1 if index < extra else 0), failed))
    return rows


class FakeCloudWatch:
    """GetMetricData over per-minute rows: missing zeroes left out as CloudWatch leaves empty minutes out,
    one page per metric when asked, so the reader has to follow NextToken."""

    def __init__(self, rows=(), pages=1, fails=None, status="Complete", extra_after_window=True, seeded=None):
        self.rows, self.pages, self.fails, self.status = list(rows), pages, fails, status
        self.seeded = dict(seeded or {})
        self.calls, self.written = [], []

    def put_metric_data(self, Namespace, MetricData):
        """As Maximum reads it: the same value twice for a minute stays that value."""
        assert len(MetricData) <= 1000 and not Namespace.startswith("AWS/")
        self.written.append((Namespace, MetricData))
        for datum in MetricData:
            minute = int(datum["Timestamp"].timestamp())
            self.seeded[minute] = max(self.seeded.get(minute, 0), datum["Value"])

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        if self.fails:
            raise self.fails
        start, end = kwargs["StartTime"].timestamp(), kwargs["EndTime"].timestamp()
        results = []
        half = len(self.rows) // self.pages
        first_page = "NextToken" not in kwargs
        for query in kwargs["MetricDataQueries"]:
            role = query["Id"]
            if role == "seeded":
                minutes = sorted(minute for minute in self.seeded if start <= minute < end)
                results.append({"Id": role, "Label": role, "StatusCode": self.status,
                                "Timestamps": [datetime.datetime.fromtimestamp(minute, datetime.timezone.utc) for minute in minutes],
                                "Values": [float(self.seeded[minute]) for minute in minutes]})
                continue
            column = {"requests": 1, "failed": 2}[role]
            rows = [row for row in self.rows if start <= row[0] < end and row[column]]
            if self.pages > 1:
                rows = rows[:half] if first_page else rows[half:]
            results.append({"Id": role, "Label": role, "StatusCode": self.status,
                            "Timestamps": [datetime.datetime.fromtimestamp(row[0], datetime.timezone.utc) for row in rows],
                            "Values": [float(row[column]) for row in rows]})
        reply = {"MetricDataResults": results}
        if self.pages > 1 and first_page:
            reply["NextToken"] = "next"
        return reply


class FakeSession:
    def __init__(self, cloudwatch):
        self.cloudwatch = cloudwatch

    def client(self, name):
        assert name == "cloudwatch"
        return self.cloudwatch


class FakeJira:
    """Jira's search as far as these queries ask, its comments and its new issues."""

    def __init__(self, issues=(), next_key=130, down=False):
        self.issues = {issue["key"]: issue for issue in issues}
        self.next_key, self.down = next_key, down
        self.created, self.comments, self.asked, self.moved = [], [], [], []

    def search(self, jql, fields="summary,status", limit=50):
        self.asked.append(jql)
        if self.down:
            raise OSError("Jira is down")
        rows = list(self.issues.values())
        if "issuetype = Bug" in jql:
            rows = [row for row in rows if row["type"] == "Bug"]
        label = re.search(r'labels = "([^"]+)"', jql)
        if label:
            rows = [row for row in rows if label.group(1) in row["labels"]]
        if "statusCategory != Done" in jql:
            rows = [row for row in rows if row["status"] != "Done"]
        if "reporter = currentUser()" in jql:
            rows = [row for row in rows if row["reporter"] == "Release Pipeline"]
        summary = re.search(r'summary ~ "\\"(.+?)\\""', jql)
        if summary:
            rows = [row for row in rows if summary.group(1) in row["summary"]]
        return [{"key": row["key"], "fields": {"summary": row["summary"], "status": {"name": row["status"]}}} for row in rows]

    def create(self, kind, summary, description, labels=(), due=None, fix_version=None):
        if self.down:
            raise OSError("Jira is down")
        key = f"ORD-{self.next_key}"
        self.next_key += 1
        self.issues[key] = {"key": key, "type": kind, "summary": summary, "labels": list(labels), "status": "Backlog",
                            "reporter": "Release Pipeline", "description": description}
        self.created.append(key)
        return key

    def call(self, method, path, body=None):
        if method == "POST" and path.endswith("/comment"):
            self.comments.append((path.split("/")[-2], body["body"]["content"][0]["content"][0]["text"]))
        return {"id": "1"}

    def transition(self, key, target):
        self.moved.append((key, target))
        self.issues[key]["status"] = target
        return True

    def version(self, name, create=True):
        return {"id": "v-" + name, "name": name}


def story(key, summary="Show the pickup time on the order page", **more):
    """A Story as setup.py makes it: Release Pipeline is its reporter too."""
    return {"key": key, "type": "Story", "summary": summary, "labels": ["orders-service"], "status": "Backlog",
            "reporter": "Release Pipeline", **more}


def budget_issue(key, **more):
    return {"key": key, "type": "Bug", "summary": BUDGET_SUMMARY, "labels": ["error-budget"], "status": "Backlog",
            "reporter": "Release Pipeline", **more}


def release_state(key="ORD-120", option="", version="1.28.0", **extra):
    state = {"service": "orders-service", "version": version, "commit": "c" * 40, "jira_key": key, "jira_keys": [key],
             "option": option, "requested_by": SAM, "slack": {"channel": "C0RELEASES", "ts": "1793.0001"},
             "build": {"number": "90", "url": "https://jenkins-demo.es.nl/job/release/90/"}, "started_at": NOW - 5,
             "stages": {name: {"status": "waiting"} for name in run_state.STAGES}, **extra}
    run_state.save(state)
    return state
