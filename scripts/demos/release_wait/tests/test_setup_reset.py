"""setup.py and reset.py against fakes. Nothing here leaves the machine."""

import importlib.util
import subprocess

import pytest

import setup as wait_setup
from conftest import HERE


class FakeJira:
    def __init__(self, issues=()):
        self.issues = {key: summary for key, summary in issues}
        self.next_key, self.created = 140, []

    def search(self, jql, fields="summary,status", limit=50):
        return [{"key": key, "fields": {"summary": summary}} for key, summary in self.issues.items()
                if summary.split(" order page")[0] in jql or summary in jql]

    def create(self, kind, summary, description, labels=(), due=None, fix_version=None):
        assert kind == "Story" and labels == ["orders-service"]
        key = f"ORD-{self.next_key}"
        self.next_key += 1
        self.issues[key] = summary
        self.created.append(key)
        return key


def load_reset():
    spec = importlib.util.spec_from_file_location("wait_reset", HERE / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_makes_the_two_stories_once_and_prints_their_keys(capsys):
    jira = FakeJira()
    wait_setup.stories(jira)
    assert jira.created == ["ORD-140", "ORD-141"]
    out = capsys.readouterr().out
    assert "ORD-[a] is ORD-140: Show the returns policy on the order page (made now)" in out
    assert "ORD-[b] is ORD-141: Show the order cut-off on the order page (made now)" in out
    wait_setup.stories(jira)
    assert jira.created == ["ORD-140", "ORD-141"] and "there already" in capsys.readouterr().out
    assert wait_setup.story_keys(jira) == {"a": "ORD-140", "b": "ORD-141"}


def test_setup_finds_only_the_exact_summary_and_a_dry_run_makes_nothing(capsys):
    jira = FakeJira([("ORD-100", "Show the returns policy on the order page, and more")])
    assert wait_setup.find_story(jira, "Show the returns policy on the order page") is None
    wait_setup.stories(jira, dry_run=True)
    assert jira.created == [] and "would make" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="run scripts/demos/release_wait/setup.py once"):
        wait_setup.story_keys(jira)


class ResetWorld:
    """The shared reset's steps recorded, in order."""

    def __init__(self, monkeypatch, main="1.28.0"):
        self.reset = load_reset()
        self.calls = []
        shared = self.shared = self.reset.shared_reset()
        world = self
        monkeypatch.setattr(self.reset, "shared_reset", lambda: shared)
        for name in ("stop_runs", "cancel_changes", "github_takes", "jfrog_takes", "lambda_aliases", "warm_slack_handler",
                     "jira_versions", "switches_off"):
            monkeypatch.setattr(shared, name, lambda *args, n=name: world.calls.append(n))
        monkeypatch.setattr(shared, "release_jobs", lambda: [])
        monkeypatch.setattr(self.reset, "stories", lambda dry_run=False: {"a": "ORD-140", "b": "ORD-141"})
        monkeypatch.setattr(self.reset.host, "main_version", lambda path: main)
        monkeypatch.setattr(self.reset.host, "stage", lambda service, version: world.calls.append(f"stage {version}"))


def test_the_reset_points_the_shared_steps_at_this_video_s_versions_only():
    shared = load_reset().shared_reset()
    assert shared.BASELINE == "1.26.0"
    assert shared.after_baseline("1.33.0") and shared.after_baseline("1.33.1")
    for version in ("1.26.0", "1.3.0", "1.4.0", "1.27.0", "1.30.0", "1.32.1", "1.33.2", "1.34.0"):
        assert not shared.after_baseline(version), version


def test_a_reset_frees_the_takes_stages_1_33_0_and_turns_the_switches_off(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    assert world.reset.run(False, "https://handler.example") == 0
    assert world.calls == ["stop_runs", "cancel_changes", "github_takes", "jfrog_takes", "jira_versions", "stage 1.33.0",
                           "lambda_aliases", "warm_slack_handler", "switches_off"]
    assert world.shared.DEMO_ISSUES == ["ORD-140", "ORD-141"]
    assert "Ready for a take." in capsys.readouterr().out


def test_the_reset_is_safe_to_run_twice(monkeypatch, capsys):
    world = ResetWorld(monkeypatch, main="1.33.0")
    assert world.reset.run(False, None) == 0
    out = capsys.readouterr().out
    assert "stage 1.33.0" not in world.calls and "main has orders-service 1.33.0" in out and "0 action(s)" in out


def test_a_dry_run_changes_nothing(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    assert world.reset.run(True, None) == 0
    out = capsys.readouterr().out
    assert "would stage orders-service 1.33.0 on main (main has 1.28.0)" in out
    assert "stage 1.33.0" not in world.calls and "Ready for a take" not in out and "1 action(s) to take" in out


def test_the_reset_makes_the_stories_if_setup_hasn_t_run(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    jira = FakeJira()
    monkeypatch.setattr(world.reset, "stories", load_reset().stories)
    monkeypatch.setattr("jira.Jira", lambda: jira)
    assert world.reset.run(False, None) == 0
    assert jira.created == ["ORD-140", "ORD-141"] and world.shared.DEMO_ISSUES == ["ORD-140", "ORD-141"]
    assert "jira_versions" in world.calls


def test_a_dry_run_before_setup_makes_no_story_and_reopens_nothing(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    jira = FakeJira()
    monkeypatch.setattr(world.reset, "stories", load_reset().stories)
    monkeypatch.setattr("jira.Jira", lambda: jira)
    assert world.reset.run(True, None) == 0
    assert jira.created == [] and "jira_versions" not in world.calls and "would make" in capsys.readouterr().out


def test_a_failed_step_says_so(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)

    def boom(reset):
        raise RuntimeError("no Lambda version of 1.26.0")

    monkeypatch.setattr(world.shared, "lambda_aliases", boom)
    assert world.reset.run(False, None) == 1
    assert "these steps failed: 7. Lambda aliases" in capsys.readouterr().out


def test_the_reset_is_found_by_its_name_with_a_dash(monkeypatch):
    import reset as shared_main

    calls = []
    monkeypatch.setattr(shared_main.subprocess, "run", lambda command, cwd: calls.append(command[1]) or
                        subprocess.CompletedProcess(command, 0))
    assert shared_main.other_demo("release-wait", ["--dry-run"]) == 0
    assert calls == [str(HERE / "reset.py")]


def test_the_docstring_warns_that_any_reset_stops_a_waiting_run():
    text = (HERE / "reset.py").read_text()
    assert "stops a release run that is waiting for its approval" in text
