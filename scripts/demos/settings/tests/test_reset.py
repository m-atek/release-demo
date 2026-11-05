"""reset.py settings: where it puts things, that it looks before it acts, and that a dry run changes nothing."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import setting as steps
from conftest import BASELINE, KNOWN, FakeAppConfig

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("settings_reset", HERE / "reset.py")
reset_py = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_py)


class Reset:
    def __init__(self, dry_run=False):
        self.dry_run, self.actions, self.failed, self.said = dry_run, 0, [], []

    def act(self, what, do):
        self.actions += 1
        self.said.append(what)
        if not self.dry_run:
            do()


def test_the_setting_at_its_baseline_needs_nothing():
    client, reset = FakeAppConfig(), Reset()
    reset_py.settings_at_baseline(reset, client, KNOWN)
    assert reset.actions == 0 and client.calls == []


def test_a_changed_setting_goes_back_with_no_bake_and_only_where_it_differs():
    client = FakeAppConfig()
    client.versions[2] = json.dumps({"free_delivery_until": "2026-11-30"}).encode()
    client.deployments["env-p"].insert(0, client.done(2, 2))
    reset = Reset()
    reset_py.settings_at_baseline(reset, client, KNOWN)
    assert reset.actions == 1 and json.loads(client.served("production")) == {"free_delivery_until": BASELINE}
    start = next(call for name, call in client.calls if name == "start")
    assert start["DeploymentStrategyId"] == "quick1" and start["EnvironmentId"] == "env-p"
    assert start["Description"] == reset_py.DESCRIPTION
    again = Reset()
    reset_py.settings_at_baseline(again, client, KNOWN)
    assert again.actions == 0


def test_after_a_move_it_waits_for_the_containers_to_read_the_file(monkeypatch):
    waited = []
    monkeypatch.setattr(steps, "pause", waited.append)
    client = FakeAppConfig()
    client.versions[2] = json.dumps({"free_delivery_until": "2026-11-31"}).encode()
    client.deployments["env-s"].insert(0, client.done(2, 2))
    reset_py.settings_at_baseline(Reset(), client, KNOWN)
    assert waited == [reset_py.FILE_READ_SECONDS]
    reset_py.settings_at_baseline(Reset(), client, KNOWN)
    assert waited == [reset_py.FILE_READ_SECONDS], "nothing moved, so nothing to wait for"


def test_the_setting_goes_back_before_anything_calls_the_page(monkeypatch):
    """A take that died in staging leaves a date that breaks the page, and the aliases' step fails on a page that doesn't answer."""
    order = []
    shared = SimpleNamespace(
        Reset=lambda dry_run: _Runner(), stop_runs=lambda reset, jobs: None, release_jobs=lambda: [], cancel_changes=lambda reset: None,
        switches_off=lambda reset: None, HistoryRunning=RuntimeError, lambda_aliases=lambda reset: order.append("aliases"),
        warm_slack_handler=lambda reset, url: order.append("slack"))
    monkeypatch.setattr(reset_py, "shared_reset", lambda: shared)
    monkeypatch.setattr(reset_py, "story_key", lambda reset: "ORD-120")
    monkeypatch.setattr(reset_py, "has_baseline", lambda: True)
    monkeypatch.setattr(reset_py, "stage", lambda reset: order.append("stage"))
    monkeypatch.setattr(reset_py, "settings_at_baseline", lambda reset: order.append("setting"))
    assert reset_py.run(False, None) == 0
    assert order == ["stage", "setting", "aliases", "slack"]


def test_a_running_deployment_is_stopped_first():
    client = FakeAppConfig()
    client.versions[2] = json.dumps({"free_delivery_until": "2026-11-30"}).encode()
    client.deployments["env-p"].insert(0, client.done(2, 2, "BAKING"))
    client.bakes_forever = True
    reset = Reset()
    reset_py.settings_at_baseline(reset, client, KNOWN)
    assert reset.said[0] == "stop AWS AppConfig deployment 2 in production"
    assert ("stop", 2) in client.calls


def test_a_dry_run_changes_nothing():
    client = FakeAppConfig()
    client.versions[2] = json.dumps({"free_delivery_until": "2026-11-30"}).encode()
    client.deployments["env-s"].insert(0, client.done(2, 2))
    reset = Reset(dry_run=True)
    reset_py.settings_at_baseline(reset, client, KNOWN)
    assert reset.actions == 1 and "put staging back" in reset.said[0]
    assert not [call for call in client.calls if call[0] in ("create", "start", "stop")]


class FakeJira:
    def __init__(self, found=()):
        self.found, self.created = list(found), []

    def search(self, jql, fields="summary,status", limit=50):
        return self.found

    def create(self, kind, summary, description, labels=(), **kwargs):
        self.created.append((kind, summary, tuple(labels)))
        return "ORD-120"


def test_the_story_is_found_by_its_exact_summary():
    jira = FakeJira([{"key": "ORD-9", "fields": {"summary": "Show the free delivery date on the order page, again"}},
                     {"key": "ORD-119", "fields": {"summary": reset_py.STORY}}])
    assert reset_py.story_key(Reset(), jira) == "ORD-119" and jira.created == []


def test_the_story_is_made_when_it_isnt_there():
    jira = FakeJira()
    reset = Reset()
    assert reset_py.story_key(reset, jira) == "ORD-120"
    assert jira.created == [("Story", reset_py.STORY, ("orders-service",))]


def test_a_dry_run_makes_no_story():
    jira = FakeJira()
    assert reset_py.story_key(Reset(dry_run=True), jira) == "ORD-?"
    assert jira.created == []


def test_the_shared_reset_is_pointed_at_this_video():
    shared = reset_py.shared_reset()
    assert shared.BASELINE == "1.31.0" and shared.JOBS == ["release", "rollback", "setting"]


def test_it_stops_when_the_first_release_fails(monkeypatch):
    monkeypatch.setattr(reset_py, "has_baseline", lambda: False)
    shared = SimpleNamespace(
        Reset=lambda dry_run: _Runner(), stop_runs=lambda reset, jobs: None, release_jobs=lambda: [], cancel_changes=lambda reset: None,
        switches_off=lambda reset: None, HistoryRunning=RuntimeError, lambda_aliases=lambda reset: pytest.fail("no aliases without a version"),
        warm_slack_handler=lambda reset, url: None)
    monkeypatch.setattr(reset_py, "shared_reset", lambda: shared)
    monkeypatch.setattr(reset_py, "story_key", lambda reset: "ORD-120")

    def failing(reset, key):
        raise RuntimeError("the release ended FAILURE")

    monkeypatch.setattr(reset_py, "release_baseline", failing)
    assert reset_py.run(False, None) == 1


class _Runner:
    dry_run, actions = False, 0

    def __init__(self):
        self.failed = []

    def act(self, what, do):
        do()

    def step(self, title, body):
        try:
            body()
        except Exception as error:
            self.failed.append(title)
            print(error)
