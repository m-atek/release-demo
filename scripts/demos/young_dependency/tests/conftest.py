"""What video 30's tests share: the run state in a temporary folder, a fake PyPI and a fake pip. Nothing here leaves the machine.

    .venv/bin/python -m pytest -q -p no:cacheprovider scripts/demos/young_dependency/tests
"""

import datetime
import json
import os
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

import package_age  # noqa: E402
import run_state  # noqa: E402

# Friday 6 November 2026, 21:32:04 in Amsterdam (CET, UTC+1).
NOW = datetime.datetime(2026, 11, 6, 20, 32, 4, tzinfo=datetime.timezone.utc).timestamp()
SAM = "U0SAM000001"
HOUR = 3600
WHEEL = "py3-none-any.whl"


def stamp(hours_ago, now=NOW):
    moment = datetime.datetime.fromtimestamp(now - hours_ago * HOUR, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


class Index:
    """PyPI's two JSON pages over {package: {version: hours ago}}: a version's page and the whole project's.
    A value may be a list of file entries, for a yanked file, an sdist or another platform's wheel."""

    def __init__(self, packages, now=NOW):
        self.packages, self.now, self.calls = packages, now, []

    def files(self, package, version):
        value = self.packages[package][version]
        if isinstance(value, list):
            return value
        return [{"filename": f"{package}-{version}-{WHEEL}", "upload_time_iso_8601": stamp(value, self.now),
                 "digests": {"sha256": sha(package, version)}}]

    def __call__(self, url):
        self.calls.append(url)
        parts = url.removeprefix(package_age.INDEX + "/").removesuffix("/json").split("/")
        if parts[0] not in self.packages:
            raise package_age.NoReading("PyPI didn't answer")
        if len(parts) == 2:
            return {"urls": self.files(parts[0], parts[1])}
        return {"releases": {version: self.files(parts[0], version) for version in self.packages[parts[0]]}}


def sha(package, version):
    return f"sha-{package}-{version}"


def resolved(*pairs):
    """What pip would install: Resolved rows, each with the checksum the fake index lists."""
    return [package_age.Resolved(package, version, f"{package}-{version}-{WHEEL}", sha(package, version)) for package, version in pairs]


SEVEN = [("boto3", "1.43.125"), ("botocore", "1.43.125"), ("jmespath", "1.1.0"), ("python-dateutil", "2.9.0.post0"),
         ("s3transfer", "0.19.3"), ("six", "1.17.0"), ("urllib3", "2.8.0")]
OLDER = [("boto3", "1.43.121"), ("botocore", "1.43.121"), *SEVEN[2:]]


def real_index():
    """A day with the SDK's newest version an hour old, others younger than the rule, and 1.43.121 three days and an hour."""
    sdk = {"1.43.125": 1.1, "1.43.124": 25, "1.43.123": 49, "1.43.122": 71.5, "1.43.121": 73, "1.43.120": 97}
    return Index({"boto3": dict(sdk), "botocore": dict(sdk), "jmespath": {"1.1.0": 6900}, "python-dateutil": {"2.9.0.post0": 23500},
                  "s3transfer": {"0.19.3": 400}, "six": {"1.17.0": 16800}, "urllib3": {"2.8.0": 1200}})


def state_for(option="standard", key="ORD-109"):
    return {"service": "orders-service", "version": "1.32.0", "jira_key": key, "option": option, "requested_by": SAM,
            "slack": {"channel": "C0RELEASES", "ts": "1.2"}, "build": {"number": 120, "url": "https://jenkins.example/job/release/120/"},
            "started_at": NOW - 10, "stages": {package_age.STAGE: {"status": "running"}, "Build": {"status": "waiting"}}}


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


@pytest.fixture(autouse=True)
def nothing_leaves_the_machine(monkeypatch):
    """No test reaches PyPI, OSV, Slack or a real AWS session, whichever way it ends up calling them."""
    import urllib.request

    import boto3

    def refused(*args, **kwargs):
        raise AssertionError("A test reached outside: fake that call.")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    monkeypatch.setattr(boto3, "Session", refused)
    monkeypatch.setattr(boto3, "client", refused)


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    """What the stage would post: the rendered lines, kept for the test."""
    import notify

    posted = []
    monkeypatch.setattr(notify, "post", lambda state: posted.append(list(state.get("lines", []))))
    return posted


@pytest.fixture
def report():
    return lambda: json.loads((run_state.REPORTS / package_age.REPORT).read_text())
