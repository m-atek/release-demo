"""reset.py two-ways: the shared steps, pointed at this video's version only."""

import sys

import pytest

import host
from conftest import HERE, by_path


class Jira:
    def __init__(self, found=None):
        self.found, self.made = found, []

    def search(self, jql, fields=None):
        assert "Show the return window on the order page" in jql
        return self.found or []

    def create(self, kind, summary, description, labels=(), due=None, fix_version=None):
        self.made.append((kind, summary, list(labels), fix_version))
        return "ORD-201"


@pytest.fixture
def module():
    return by_path("two_ways_reset", HERE / "reset.py")


class Recorder:
    """The shared module's steps, recorded in order: what each stands for is scripts/tests/test_reset.py's."""

    def __init__(self, real, fail_at=None, history_running=False):
        self.real, self.order, self.fail_at, self.history_running = real, [], fail_at, history_running
        self.Reset = real.Reset
        self.HistoryRunning = real.HistoryRunning
        self.DEMO_ISSUES = list(real.DEMO_ISSUES)
        self.BASELINE = real.BASELINE

    def __getattr__(self, name):
        def step(*args, **kwargs):
            self.order.append(name)
            if name == "stop_runs" and self.history_running:
                raise self.real.HistoryRunning("history.py is running")
            if name == self.fail_at:
                raise RuntimeError(f"{name} failed")
            return []
        return step


def test_the_shared_steps_see_only_this_video_s_version(module):
    shared = module.shared_reset()
    assert shared.BASELINE == "1.26.0"
    assert shared.after_baseline("1.30.0")
    for other in ("1.4.0", "1.12.2", "1.26.0", "1.28.1", "1.31.0"):
        assert not shared.after_baseline(other)


def run_with(module, monkeypatch, recorder, jira, main_version="1.3.0", dry_run=False):
    stages = []
    monkeypatch.setattr(module, "shared_reset", lambda: recorder)
    monkeypatch.setattr(module.host, "main_version", lambda path: main_version)
    monkeypatch.setattr(module.host, "stage", lambda name, version: stages.append((name, version)))
    original = module.story
    monkeypatch.setattr(module, "story", lambda reset, shared: original(reset, shared, jira))
    return module.run(dry_run, "https://handler.example/"), stages


def test_a_reset_runs_every_step_in_order_and_makes_the_story_once(module, monkeypatch, capsys):
    recorder = Recorder(module.shared_reset())
    jira = Jira()
    code, stages = run_with(module, monkeypatch, recorder, jira)
    assert code == 0
    assert recorder.order == ["release_jobs", "stop_runs", "cancel_changes", "github_takes", "jfrog_takes",
                              "switches_off", "lambda_aliases", "warm_slack_handler"]
    assert jira.made == [("Story", "Show the return window on the order page", ["orders-service"], "orders-service 1.30.0")]
    assert stages == [("orders-service", "1.30.0")]
    out = capsys.readouterr().out
    assert "ORD-[s] is ORD-201: Show the return window on the order page (made now)" in out
    assert "Ready for a take: production on 1.26.0, 1.30.0 on main, ORD-[s] is ORD-201." in out


def test_a_second_reset_finds_the_story_and_main_and_turns_nothing_on(module, monkeypatch, capsys):
    recorder = Recorder(module.shared_reset())
    found = {"key": "ORD-201", "fields": {"summary": "Show the return window on the order page", "status": {"name": "Backlog"}}}
    jira = Jira([found])
    code, stages = run_with(module, monkeypatch, recorder, jira, main_version="1.30.0")
    assert code == 0 and stages == [] and jira.made == []
    assert recorder.DEMO_ISSUES == ["ORD-201"] and "jira_versions" in recorder.order
    assert "main has orders-service 1.30.0" in capsys.readouterr().out
    assert "set(" not in recorder.order and "put_parameter" not in recorder.order


def test_a_similar_summary_isn_t_the_story(module):
    close = {"key": "ORD-9", "fields": {"summary": "Show the return window on the order page, later", "status": {"name": "Backlog"}}}
    assert module.find_story(Jira([close])) is None


def test_a_dry_run_makes_and_stages_nothing(module, monkeypatch, capsys):
    recorder = Recorder(module.shared_reset())
    jira = Jira()
    code, stages = run_with(module, monkeypatch, recorder, jira, dry_run=True)
    assert code == 0 and stages == [] and jira.made == []
    out = capsys.readouterr().out
    assert "would make the Story" in out and "would stage orders-service 1.30.0 on main" in out
    assert "Dry run in" in out and "Ready for a take" not in out


def test_a_failed_step_says_so_and_a_running_history_stops_it(module, monkeypatch, capsys):
    code, _ = run_with(module, monkeypatch, Recorder(module.shared_reset(), fail_at="lambda_aliases"), Jira())
    assert code == 1
    assert "these steps failed: 8. Lambda aliases on 1.26.0, and one call each" in capsys.readouterr().out
    code, _ = run_with(module, monkeypatch, Recorder(module.shared_reset(), history_running=True), Jira())
    assert code == 1


def test_from_the_mac_it_hands_over_and_reset_py_finds_it_by_its_dash(module, monkeypatch):
    calls = []
    monkeypatch.setattr(host, "on_host", lambda: False)
    monkeypatch.setattr(host, "hand_over", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    monkeypatch.setattr(sys, "argv", ["reset.py", "--dry-run", "--slack-handler-url", "https://handler.example/"])
    assert module.main() == 0
    assert calls == [(("reset", "demos/two_ways/reset.py", ["--dry-run", "--slack-handler-url", "https://handler.example/"]),
                      {"title": "reset.py two-ways --dry-run"})]
    assert (HERE.parent / "two-ways".replace("-", "_") / "reset.py").exists()
