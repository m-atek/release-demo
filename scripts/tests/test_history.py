"""history.py against a fake Jenkins: it releases the plan in order, resumes,
and stops on a run that ends other than planned."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import history  # noqa: E402
from history import LIVE, PLAN, ROLLED_BACK  # noqa: E402

SAM = "U0SAMSAMSAM"


def finished(number, version, key, result, building=False):
    return {"number": number, "displayName": f"#{number} orders-service {version}", "result": result,
            "building": building, "parameters": {"SERVICE": "orders-service", "JIRA_KEY": key}}


class FakeJob:
    """The release job: each started run finishes as planned unless told otherwise."""

    def __init__(self, builds=(), outcomes=None):
        self.runs = {b["number"]: dict(b) for b in builds}
        self.outcomes = outcomes or {}  # version -> result for runs started here
        self.started_params = []
        self.consoles = {}
        self.polls = {}
        self.waiting = []

    def builds(self, limit=50):
        return sorted(self.runs.values(), key=lambda b: -b["number"])

    def queued(self):
        return list(self.waiting)

    def start(self, params):
        self.started_params.append(params)
        return 1000 + len(self.started_params)

    def started(self, queue_id, waiting=None):
        params = self.started_params[queue_id - 1001]
        version, _, expect = next(step for step in PLAN if step[1] == params["JIRA_KEY"])
        number = max(self.runs, default=0) + 1
        result = self.outcomes.get(version, "SUCCESS" if expect == LIVE else "FAILURE")
        self.runs[number] = finished(number, version, params["JIRA_KEY"], result, building=True)
        if result == "FAILURE" and expect == ROLLED_BACK and version not in self.outcomes:
            self.consoles[number] = "ERROR: Production check failed: rolled back to 1.2.3."
        return number

    def build(self, number):
        # Running on the first look, finished on the second.
        self.polls[number] = self.polls.get(number, 0) + 1
        if self.polls[number] > 1:
            self.runs[number]["building"] = False
        return dict(self.runs[number])

    def stages(self, number):
        return [("Build", "SUCCESS", 12.0), ("Test", "SUCCESS", 3.2)]

    def console(self, number):
        return self.consoles.get(number, "Finished: FAILURE")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    staged = []
    monkeypatch.setattr(history.host, "stage", lambda service, version: staged.append((service, version)))
    monkeypatch.setattr(history.host, "main_version", lambda path: "1.3.0")
    monkeypatch.setattr(history.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(history, "production_version", lambda: "1.3.0")
    return staged


def test_releases_the_whole_plan_in_order_as_sam_with_standard_changes(quiet, capsys):
    job = FakeJob()
    history.run(job, SAM)
    assert quiet == [("orders-service", version) for version, _, _ in PLAN]
    assert [p["JIRA_KEY"] for p in job.started_params] == [key for _, key, _ in PLAN]
    assert all(p["OPTION"] == "standard" and p["REQUESTED_BY"] == SAM and p["SERVICE"] == "orders-service"
               and p["SLACK_TS"] == "" for p in job.started_params)
    out = capsys.readouterr().out
    assert out.count("rolled back, as planned") == 2
    assert "Production runs orders-service 1.3.0." in out


def test_resumes_skipping_what_ran_as_planned_and_waiting_for_a_running_build(quiet, capsys):
    builds = [finished(n, version, key, "SUCCESS" if expect == LIVE else "FAILURE")
              for n, (version, key, expect) in enumerate(PLAN[:5], 1)]
    builds.append(finished(6, "1.2.5", "ORD-6", "SUCCESS", building=True))
    job = FakeJob(builds)
    job.consoles[5] = "ERROR: Production check failed: rolled back to 1.2.3."
    history.run(job, SAM)
    assert [version for _, version in quiet] == ["1.2.6", "1.2.7", "1.2.8", "1.2.9", "1.3.0"]
    out = capsys.readouterr().out
    assert "build #6 is still going: waiting for it" in out
    assert out.count("done earlier") == 5


def test_a_run_that_ends_other_than_planned_stops_history(quiet):
    job = FakeJob(outcomes={"1.2.2": "FAILURE"})
    with pytest.raises(SystemExit):
        history.run(job, SAM)
    assert [version for _, version in quiet] == ["1.2.0", "1.2.1", "1.2.2"]


def test_a_planned_failure_must_be_the_rollback_not_any_failure(quiet):
    job = FakeJob(outcomes={"1.2.4": "FAILURE"})  # failed, but not at the production check
    with pytest.raises(SystemExit):
        history.run(job, SAM)
    assert quiet[-1] == ("orders-service", "1.2.4")


def test_a_planned_rollback_that_went_live_stops_history(quiet):
    job = FakeJob(outcomes={"1.2.8": "SUCCESS"})
    with pytest.raises(SystemExit):
        history.run(job, SAM)
    assert quiet[-1] == ("orders-service", "1.2.8")


def test_an_earlier_failed_run_is_released_again(quiet):
    builds = [finished(1, "1.2.0", "ORD-1", "FAILURE")]
    history.run(FakeJob(builds), SAM)
    assert quiet[0] == ("orders-service", "1.2.0")


def test_a_run_of_another_version_under_the_same_key_doesnt_count(quiet):
    builds = [finished(1, "1.4.0", "ORD-1", "SUCCESS")]
    history.run(FakeJob(builds), SAM)
    assert quiet[0] == ("orders-service", "1.2.0")


def test_dry_run_stages_and_starts_nothing(quiet, capsys):
    builds = [finished(1, "1.2.0", "ORD-1", "SUCCESS")]
    job = FakeJob(builds)
    history.run(job, SAM, dry_run=True)
    assert quiet == [] and job.started_params == []
    out = capsys.readouterr().out
    assert "done earlier: build #1, live" in out
    assert out.count("to do: stage") == len(PLAN) - 1


def test_dry_run_with_everything_done_says_so(quiet, capsys):
    builds = [finished(n, version, key, "SUCCESS" if expect == LIVE else "FAILURE")
              for n, (version, key, expect) in enumerate(PLAN, 1)]
    job = FakeJob(builds)
    for number, (_, _, expect) in enumerate(PLAN, 1):
        if expect == ROLLED_BACK:
            job.consoles[number] = "ERROR: Production check failed: rolled back to 1.2.7."
    history.run(job, SAM, dry_run=True)
    assert "Nothing to do." in capsys.readouterr().out


def test_ends_only_with_production_on_the_baseline(quiet, monkeypatch):
    monkeypatch.setattr(history, "production_version", lambda: "1.2.9")
    with pytest.raises(SystemExit):
        history.run(FakeJob(), SAM)


def test_hands_itself_to_the_tools_host_from_the_mac(monkeypatch):
    calls = []
    monkeypatch.setattr(history.host, "on_host", lambda: False)
    monkeypatch.setattr(history.host, "hand_over", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    monkeypatch.setattr("sys.argv", ["history.py", "--dry-run"])
    assert history.main() == 0
    assert calls == [(("history", "history.py", ["--dry-run"]), {"follow_only": False, "interval": 10})]


def test_a_run_waiting_in_the_queue_is_waited_for_not_started_again(quiet, capsys):
    builds = [finished(1, "1.2.0", "ORD-1", "SUCCESS")]
    job = FakeJob(builds)
    job.started_params.append({"SERVICE": "orders-service", "JIRA_KEY": "ORD-2"})  # queued before an interruption
    job.waiting = [{"id": 1001, "parameters": {"SERVICE": "orders-service", "JIRA_KEY": "ORD-2"}}]
    original = job.started

    def started(queue_id, waiting=None):
        job.waiting = []
        return original(queue_id, waiting)

    job.started = started
    history.run(job, SAM)
    assert [version for _, version in quiet] == [version for version, _, _ in PLAN[2:]]
    assert "a run for ORD-2 waits in the queue: waiting for it" in capsys.readouterr().out
