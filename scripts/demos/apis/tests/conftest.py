import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

import run_state  # noqa: E402


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


class FakeLambda:
    """The alias live and its versions, as the Lambda API answers for them."""

    def __init__(self, main="1", weights=None, versions=None):
        self.main, self.weights = main, dict(weights or {})
        self.versions = versions or {"1": "pricing-api in production. Demo system, not a client's.", "2": "2.1.1"}
        self.updates = []

    def get_alias(self, FunctionName, Name):
        reply = {"FunctionVersion": self.main}
        if self.weights:
            reply["RoutingConfig"] = {"AdditionalVersionWeights": dict(self.weights)}
        return reply

    def update_alias(self, FunctionName, Name, FunctionVersion, RoutingConfig):
        self.main, self.weights = FunctionVersion, dict(RoutingConfig["AdditionalVersionWeights"])
        self.updates.append((FunctionVersion, dict(self.weights)))


class FakeCloudWatch:
    """Alarms that go to ALARM when the test says so."""

    def __init__(self, fire_after=None, clock=None):
        self.alarms, self.deleted, self.fire_after, self.clock = {}, [], fire_after, clock

    def put_metric_alarm(self, **spec):
        self.alarms[spec["AlarmName"]] = spec

    def describe_alarms(self, AlarmNames):
        import datetime

        firing = self.fire_after is not None and self.clock() >= self.fire_after
        stamp = datetime.datetime.fromtimestamp(self.fire_after or 0, datetime.timezone.utc)
        return {"MetricAlarms": [
            {"AlarmName": name, "StateValue": "ALARM" if firing and "errors" in name else "OK",
             "StateUpdatedTimestamp": stamp, "StateReason": "Threshold Crossed: 1 datapoint [16.0] >= 3.0"}
            for name in AlarmNames if name in self.alarms]}

    def delete_alarms(self, AlarmNames):
        self.deleted += AlarmNames
        for name in AlarmNames:
            self.alarms.pop(name, None)


class Clock:
    """Time that moves only when the code under test sleeps."""

    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
