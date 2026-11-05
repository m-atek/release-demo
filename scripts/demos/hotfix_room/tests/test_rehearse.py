"""The first-day measurement's pass and fail: what each reading comes to, the verdict, and a whole run on a fake machine for each way the day can go."""

from types import SimpleNamespace

import pytest

import rehearse
import room
import run_state
from fake_node import MIB

SAID = ("0/1 nodes are available: 1 Insufficient memory. preemption: 0/1 nodes are available: "
        "1 No preemption victims found for incoming pod.")


def reading(free=22, used=256, ask=48, reserved_forecast=768, forecast_used=51, pending="", said=""):
    return room.Reading(allocatable=1180 * MIB, reserved=(1180 - free) * MIB, used=used * MIB, forecast_reserved=reserved_forecast * MIB,
                        forecast_used=forecast_used * MIB, ask=ask * MIB, pending=pending, said=said)


def test_what_kubectl_describe_node_says_is_read_from_its_own_table():
    text = """Allocated resources:
  (Total limits may be over 100 percent, i.e., overcommitted.)
  Resource           Requests      Limits
  --------           --------      ------
  cpu                230m (5%)     2500m (62%)
  memory             1158Mi (98%)  1674Mi (141%)
  ephemeral-storage  0 (0%)        0 (0%)
"""
    assert rehearse.described_memory(text) == (1158, 98)
    assert rehearse.described_memory("Capacity:\n  memory:  16072912Ki\n") is None


def test_the_schedulers_words_must_be_a_memory_refusal():
    assert rehearse.scheduler_words_hold(SAID)
    assert not rehearse.scheduler_words_hold("0/1 nodes are available: 1 Insufficient cpu.")
    assert not rehearse.scheduler_words_hold("")


def test_the_video_stands_on_a_real_pending_and_a_low_reading():
    stands, lines = rehearse.verdict(SAID, True, reading(), True)
    assert stands and lines == ["THE VIDEO STANDS."]


def test_no_pending_no_video():
    stands, lines = rehearse.verdict("", True, reading(), True)
    assert not stands and lines[0].startswith("THE VIDEO ISN'T MADE")
    stands, lines = rehearse.verdict("0/1 nodes are available: 1 Too many pods.", True, reading(), True)
    assert not stands and "Too many pods" in lines[1]


def test_no_rollback_is_a_fix_not_the_end():
    stands, lines = rehearse.verdict(SAID, False, reading(), True)
    assert not stands and lines[0].startswith("NO VIDEO UNTIL FIXED")


@pytest.mark.parametrize("used, stands, word", [(472, True, None), (473, True, "open question 2"), (708, True, "open question 2"), (709, False, "THE VIDEO ISN'T MADE")])
def test_the_share_used_decides_the_hook(used, stands, word):
    """40% of 1,180 is 472 MiB and 60% is 708 MiB."""
    verdict, lines = rehearse.verdict(SAID, True, reading(used=used), True)
    assert verdict is stands
    if word:
        assert word in " ".join(lines)
    else:
        assert lines == ["THE VIDEO STANDS."]


def test_the_same_hotfix_must_go_through_at_the_end():
    stands, lines = rehearse.verdict(SAID, True, reading(), False)
    assert not stands and "the same hotfix didn't go through" in lines[0]


def test_the_brackets_say_each_number_as_the_script_uses_it():
    after = reading(free=646, reserved_forecast=144)
    lines = rehearse.brackets(reading(), after, 183, 402)
    text = "\n".join(lines)
    assert "[22], percent used, the hook: 256 MiB used of 1180 is 22%, 1158 MiB reserved is 98%" in text
    assert "free to reserve 22 MiB, catalog's next pod asks for 48 MiB" in text
    assert "[51] in 33.7" in text
    assert "so [624] come back" in text
    assert "Helm's wait: 3:03" in text and "402 MiB" in text


# A whole run, on a fake machine.

class FakeMachine:
    """Where the day goes is set by what it's given. It records in order what the pipeline's scripts would do."""

    def __init__(self, said=SAID, rolled_back=True, second_try=True, problems=(), same_build=True):
        self.said, self.rolled_back, self.second_try, self.problems, self.same_build = said, rolled_back, second_try, problems, same_build
        self.ran, self.peak, self.builds = [], 402, 0
        self.sampling = SimpleNamespace(wait=lambda seconds: True, set=lambda: None)

    def url(self, name, environment):
        return "http://127.0.0.1:1"

    def preflight(self):
        return list(self.problems)

    def only_cluster_to_baseline(self):
        self.ran.append("baseline")

    def package(self, name, version):
        self.builds += 1
        self.ran.append(f"build {name} {version}")
        digest = "sha256:same" if self.same_build or name != "catalog" or self.builds < 3 else "sha256:other"
        state = {"service": name, "version": version}
        run_state.save(state)
        return b"zip", state, {"sha256": digest, "image_digest": digest}

    def deploy(self, name, environment, package, state):
        self.ran.append(f"deploy {name} {environment} {state['version']}")
        record = {"helm_seconds": 183.0, "lowest_ready": 3, "url": "http://127.0.0.1:1"}
        refused = name == "catalog" and environment == "staging" and "first refused" not in self.ran
        if refused:
            self.ran.append("first refused")
            if self.rolled_back:
                record.update(helm_rolled_back_to="1.12.0", helm_rollback_reason="Not ready after 3 minutes.",
                              scheduler_said=self.said.split(". ")[0] + "." if self.said else None)
        saved = run_state.load()
        saved.setdefault("deploys", {})[environment] = record
        run_state.save(saved)
        return True if refused or name == "forecast" else self.second_try

    def check(self, name, environment, version):
        self.ran.append(f"check {name} {environment}")
        return "helm_rolled_back_to" not in run_state.load()["deploys"].get(environment, {}) and (self.second_try or name == "forecast")

    def go_back(self, name, environment):
        self.ran.append("back")
        saved = run_state.load()
        saved["deploys"][environment]["rolled_back_by"] = "Helm"
        run_state.save(saved)
        return True

    def check_the_settings(self, reading_):
        rehearse.result(True, "1 and 2. the settings")

    def check_used(self, reading_):
        rehearse.result(True, "5. used")

    def can_i(self):
        rehearse.result(True, "9. can-i")

    def no_hand_edit(self, version):
        rehearse.result(True, "16. no hand edit")

    def fits_the_cgroup(self):
        rehearse.result(True, "10. fits")

    def sample_memory(self):
        pass


class FakeWatch:
    machine_said = SAID

    def __init__(self):
        self.pod, self.said, self.reading, self.pending_seen = "catalog-new", self.machine_said, reading(pending="catalog-new", said=self.machine_said), 60

    def start(self):
        pass

    def done(self):
        return self


class FakeProber:
    def __init__(self, url):
        pass

    def start(self):
        pass

    def done(self):
        return {"1.12.0": 900}, 0


@pytest.fixture
def day(monkeypatch):
    rehearse.results.clear()
    monkeypatch.setattr(rehearse, "Watch", FakeWatch)
    monkeypatch.setattr(rehearse, "Prober", FakeProber)
    readings = iter([reading(), reading(free=646, reserved_forecast=144)])
    monkeypatch.setattr(rehearse.room, "read", lambda get: next(readings, reading(free=646, reserved_forecast=144)))
    return monkeypatch


def test_a_good_day(day, capsys):
    machine = FakeMachine()
    assert rehearse.measure(machine) == 0
    out = capsys.readouterr().out
    assert "THE VIDEO STANDS." in out and "FAIL" not in out.replace("0 FAIL", "")
    assert machine.ran == ["baseline", "build catalog 1.12.1", "deploy catalog staging 1.12.1", "first refused", "check catalog staging", "back",
                           "build forecast 2.4.1", "deploy forecast production 2.4.1", "check forecast production",
                           "build catalog 1.12.1", "deploy catalog staging 1.12.1", "check catalog staging",
                           "deploy catalog production 1.12.1", "check catalog production"]


def test_a_day_with_no_pending_decides_the_video(day, capsys, monkeypatch):
    monkeypatch.setattr(FakeWatch, "machine_said", "")
    machine = FakeMachine(said="")
    assert rehearse.measure(machine) == 1
    assert "THE VIDEO ISN'T MADE" in capsys.readouterr().out


def test_a_cluster_not_set_up_says_which_command(day, capsys):
    assert rehearse.measure(FakeMachine(problems=["no namespace forecast: scripts/demo.sh up hotfix-room"])) == 1
    out = capsys.readouterr().out
    assert "FAIL Not started: no namespace forecast: scripts/demo.sh up hotfix-room" in out


def test_another_check_failing_stands_with_exit_code_2(day, capsys):
    machine = FakeMachine(same_build=False)
    assert rehearse.measure(machine) == 2
    out = capsys.readouterr().out
    assert "THE VIDEO STANDS." in out and "fix these before a take" in out


def test_a_second_try_that_fails_is_no_video_until_fixed(day, capsys):
    machine = FakeMachine(second_try=False)
    assert rehearse.measure(machine) == 1
    assert "the same hotfix didn't go through" in capsys.readouterr().out


class WatchThatSees(rehearse.Watch):
    pass


def test_the_watch_keeps_the_pending_reading_and_survives_a_missed_read(monkeypatch):
    answers = iter([RuntimeError("no answer"), reading(pending="catalog-new", said=SAID), reading(free=22)])

    def read():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rehearse, "SAMPLE_SECONDS", 0.01)
    watch = rehearse.Watch(read=read)
    watch.stop = SimpleNamespace(wait=lambda seconds, left=[False, False, False, True]: left.pop(0), set=lambda: None)
    watch.run()
    assert (watch.pod, watch.said, watch.pending_seen) == ("catalog-new", SAID, 1)
    assert watch.reading.pending == "catalog-new"
