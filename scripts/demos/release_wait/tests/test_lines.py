"""What Slack and the change say after a long wait: the two numbers add up to "Live in", and no other run changes."""

import sys

import pytest

import change
import notify
import run_state
from conftest import ANY_OTHER, WAITS

# (seconds, as run_state.duration writes them, never rounded down)
DURATIONS = [(0.4, "1s"), (42, "42s"), (59.2, "1m 00s"), (402, "6m 42s"), (3599, "59m 59s"), (3599.1, "1h 00m 00s"),
             (3600, "1h 00m 00s"), (3661, "1h 01m 01s"), (21434.2, "5h 57m 15s"), (36000, "10h 00m 00s")]


@pytest.mark.parametrize("seconds, written", DURATIONS)
def test_duration_writes_hours_from_an_hour_on(seconds, written):
    assert run_state.duration(seconds) == written


@pytest.fixture
def finished(monkeypatch):
    """notify.py finish, as the pipeline calls it: a state on disk, the clock at started_at + elapsed, Slack recorded."""
    posted = []
    monkeypatch.setattr(notify, "post", lambda state: posted.append(state))

    def run(result, elapsed, approval_seconds=None, **extra):
        stages = {"Approval": {"status": "ok", "seconds": approval_seconds} if approval_seconds else {"status": "skipped"}}
        run_state.save({"service": "orders-service", "version": WAITS, "started_at": 1793000000.0, "stages": stages,
                        "jira_key": "ORD-140", "build": {}, "slack": {"channel": "C", "ts": "1"}, **extra})
        monkeypatch.setattr(run_state, "now", lambda: 1793000000.0 + elapsed)
        monkeypatch.setattr(sys, "argv", ["notify.py", "finish", result])
        notify.main()
        return run_state.load()

    return run


def test_after_a_long_wait_slack_says_how_the_total_splits(finished):
    state = finished("live", 21662.5, 21433.7, long_wait=True)
    assert state["total"] == "6h 01m 03s"
    assert state["lines"] == ["Live in 6h 01m 03s.", "Waited for approval: 5h 57m 14s.\nEverything else: 3m 49s."]


@pytest.mark.parametrize("elapsed, approval", [(21662.5, 21433.7), (21600.0, 21401.0), (1985.2, 1801.9), (36000.0, 35700.0), (3700.9, 3600.1)])
def test_the_two_numbers_add_up_to_live_in_exactly(finished, elapsed, approval):
    state = finished("live", elapsed, approval, long_wait=True)
    def seconds(text):
        parts = {unit: int(number) for number, unit in ((word[:-1], word[-1]) for word in text.split())}
        return parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)
    waited, rest = state["lines"][1].split("\n")
    assert seconds(waited.removeprefix("Waited for approval: ").rstrip(".")) + seconds(rest.removeprefix("Everything else: ").rstrip(".")) == seconds(state["total"])


def test_each_new_line_stays_whole_in_a_4_5_crop(finished):
    state = finished("live", 36000.0, 35700.0, long_wait=True)
    assert all(len(line) <= 53 for text in state["lines"] for line in text.split("\n"))


def test_the_same_lines_go_on_the_change_in_front_of_its_notes(finished):
    state = finished("live", 21662.5, 21433.7, long_wait=True)
    assert state["close_notes_extra"] == "Waited for approval: 5h 57m 14s.\nEverything else: 3m 49s."

    class Sn:
        calls = []

        def call(self, method, path, body=None, **query):
            self.calls.append(body)

    state["change"] = {"sys_id": "change-id", "type": "normal"}
    change.close(Sn(), state, "successful", "Released orders-service 1.33.0. Total time: 6h 01m 03s. Build: x")
    assert Sn.calls[-1]["close_notes"] == ("Waited for approval: 5h 57m 14s.\nEverything else: 3m 49s.\n"
                                           "Released orders-service 1.33.0. Total time: 6h 01m 03s. Build: x")


def test_a_release_with_no_long_wait_reads_as_it_always_has(finished):
    state = finished("live", 162.4, version=ANY_OTHER)
    assert state["lines"] == ["Live in 2m 43s."] and "close_notes_extra" not in state
    state = finished("live", 263.1, 58.2, version="1.4.0")
    assert state["lines"] == ["Live in 4m 24s."] and "close_notes_extra" not in state


@pytest.mark.parametrize("result, line", [("failed", "Stopped after 6h 01m 03s. Nothing reached production."),
                                          ("cancelled", "Cancelled after 6h 01m 03s.")])
def test_a_long_wait_that_doesn_t_go_live_adds_no_split(finished, result, line):
    state = finished(result, 21662.5, 21433.7, long_wait=True)
    assert state["lines"] == [line] and "close_notes_extra" not in state


def test_the_rows_write_a_long_approval_as_a_person_would(finished):
    state = finished("live", 21662.5, 21433.7, long_wait=True)
    assert "Approval  _5h 57m 14s_" in notify.render(state)
