"""The first-day measurement's pass and fail: what each block comes to, the ladder, the verdict,
and a whole run on a fake machine for each way the day can go."""

import json
import re
from argparse import Namespace

import pytest

import rehearse
from common import ROOT


def pods(restarts_after=1, crash=False, not_ready=True, count=3):
    return {f"catalog-a-{i}": {"restarts_before": 0, "restarts_after": restarts_after, "failed_liveness_at": 25.0 + i,
                               "restarted_at": 31.0 + i, "crash_loop_back_off": crash, "was_not_ready": not_ready}
            for i in range(count)}


def blip(version="1.10.0", **changes):
    fixed = version == "1.10.1"
    values = {"version": version, "passed": fixed, "restarted": 0 if fixed else 3, "replicas": 3, "seconds": 30,
              "pods": pods(0, not_ready=False) if fixed else pods(), "all_not_ready_at": None if fixed else 9.4,
              "back_at": 31 if fixed else 38, "block_seconds": 30.02,
              "calls": {"/api/products": {"200": 100, "503": 300}, "/health": {"200": 400}},
              "calls_inside_block": {"/api/products": {"503": 300}, "/health": {"200": 300}},
              "data_in_a_503": 0, "times": {"apply": -0.4, "delete_returned": 30.02, "first_success_after_delete": 30.3}}
    return {**values, **changes}


def test_the_gate_wants_every_pod_restarted_once():
    every, down, line = rehearse.failing_block(blip())
    assert every and down
    assert "3 of 3 restarted" in line and "all not ready at 0:09" in line and "back at 0:38" in line and "block 30.02 s" in line


@pytest.mark.parametrize("changes", [{"restarted": 2}, {"pods": pods(2)}, {"pods": pods(crash=True)},
                                     {"pods": pods(count=2), "restarted": 2}])
def test_the_gate_refuses_fewer_a_second_restart_and_a_crash_loop(changes):
    assert rehearse.failing_block(blip(**changes))[0] is False


def test_all_down_at_once_is_a_separate_answer():
    every, down, _ = rehearse.failing_block(blip(all_not_ready_at=None))
    assert every and not down


ROUTES = ("/api/products", "/health")


def test_the_fix_passes_with_only_503s_and_every_pod_up():
    ok, line = rehearse.fixed_block(blip("1.10.1"), *ROUTES)
    assert ok
    assert "0 of 3 restarted" in line and "READY true throughout: True" in line and "back at 0:31" in line


@pytest.mark.parametrize("changes", [
    {"restarted": 1},
    {"pods": pods(0, not_ready=True)},
    {"calls": {"/api/products": {"200": 100, "503": 300}, "/health": {"200": 399, "refused": 1}}},
    {"calls": {"/api/products": {"200": 100, "503": 299, "refused": 1}, "/health": {"200": 400}}},
    {"calls": {"/api/products": {"200": 100, "503": 299, "timeout": 1}, "/health": {"200": 400}}},
    {"calls_inside_block": {"/api/products": {"503": 299, "200": 1}, "/health": {"200": 300}}},
    {"back_at": 33},
    {"back_at": None},
    {"data_in_a_503": 1},
    {"passed": False},
])
def test_the_fix_fails_on_each_of_check_5s_rules(changes):
    assert rehearse.fixed_block(blip("1.10.1", **changes), *ROUTES)[0] is False


ALL = [(True, True)] * 3


def test_verdict_it_stands():
    stands, lines = rehearse.verdict(ALL, None, [True] * 3)
    assert stands and lines == ["THE VIDEO STANDS."]


def test_verdict_it_stands_with_periodseconds_5():
    stands, lines = rehearse.verdict([(False, True)] * 3, ALL, [True] * 3)
    assert stands and lines[0] == "THE VIDEO STANDS."
    assert any("periodSeconds: 5" in line for line in lines) and any('"Two lines"' in line for line in lines)


def test_verdict_no_video_when_periodseconds_5_doesnt_help():
    stands, lines = rehearse.verdict([(False, True)] * 3, [(False, True)] * 3, None)
    assert not stands
    assert lines[0].startswith("THE VIDEO ISN'T MADE") and "periodSeconds 5 didn't help" in lines[0]
    assert any("no failureThreshold: 1" in line for line in lines)


def test_verdict_no_video_until_the_fix_holds():
    stands, lines = rehearse.verdict(ALL, None, [True, False, True])
    assert not stands
    assert any(line.startswith("NO VIDEO UNTIL FIXED") for line in lines)
    assert any("CPU throttling" in line for line in lines)


def test_verdict_every_pod_restarts_but_not_all_at_once():
    stands, lines = rehearse.verdict([(True, False)] * 3, None, [True] * 3)
    assert stands
    assert any("25.6 goes" in line for line in lines)


def test_the_brackets_and_their_fallbacks():
    plain = rehearse.brackets([blip(back_at=38)], [blip("1.10.1", back_at=31)])
    assert "all not ready at 0:09" in plain[0] and "0:38" in plain[1] and "0:31" in plain[2]
    assert len(plain) == 3
    close = rehearse.brackets([blip(back_at=32)], [blip("1.10.1", back_at=31)])
    assert any("within 3 s of the database" in line for line in close)
    assert any("within 2 s" in line for line in close)


def test_the_ladder_changes_one_line_of_the_real_chart():
    template = (ROOT / "demo-states/catalog/1.10.0/chart/templates/deployment.yaml").read_text()
    changed, count = re.subn(r"periodSeconds: 10\b", "periodSeconds: 5", template)
    assert count == 1
    lines = template.splitlines()
    assert lines[lines.index("          livenessProbe:") + 3].strip() == "periodSeconds: 10"


class Day:
    """A fake machine: canned blocks for each version, every other check fine."""

    def __init__(self, failing=None, ladder=None, fixed=None, problems=(), connection="failed 0.01 ConnectionRefusedError"):
        self.failing = failing or [blip() for _ in range(3)]
        self.ladder = ladder
        self.fixed = fixed or [blip("1.10.1") for _ in range(3)]
        self.problems, self.connection = list(problems), connection
        self.blip = Namespace(data_route="/api/products", alive_route="/health", namespace="staging")
        self.blocks, self.sampling, self.log = 3, rehearse.threading.Event(), []
        self.current = None

    def preflight(self):
        return self.problems

    def sample_memory(self):
        pass

    def put_on_baseline(self):
        self.log.append("baseline")

    def release(self, version, folder=None):
        self.log.append(f"release {version}")
        self.current = version
        return b"zip", {}, {}

    def deploy(self, environment, package, state, version):
        self.log.append(f"deploy {version} {environment}")
        return True

    def check_real_database(self):
        rehearse.result(True, "1. real")

    def database_log_starts(self):
        return 2

    def database_restarts(self):
        return "0"

    def blocks_on_fresh_pods(self, label, count=None):
        self.log.append(f"blocks {label}")
        return self.failing[:count] if label == "1.10.0" else self.fixed[:count]

    def one_block(self):
        self.log.append(f"block {self.current}")
        return self.failing[0] if self.current == "1.10.0" else self.fixed[0]

    def with_liveness_period(self, seconds):
        self.log.append(f"ladder {seconds}")
        return self.ladder

    def during_a_block(self):
        return {"connection": self.connection, "production": [200] * 20, "seen": True}, rehearse.threading.Event(), []

    def can_i(self):
        rehearse.result(True, "3. can-i")

    def abort_mid_block(self):
        rehearse.result(True, "13. abort")

    def check_reproducible(self):
        rehearse.result(True, "15. reproducible")

    def check_fits(self):
        rehearse.result(True, "9. fits")


@pytest.fixture(autouse=True)
def fresh_results(monkeypatch):
    monkeypatch.setattr(rehearse, "results", [])


def measure(day, **args):
    return rehearse.measure(day, Namespace(liveness_period=args.get("liveness_period")))


def test_a_good_day_exit_0(capsys):
    day = Day()
    assert measure(day) == 0
    out = capsys.readouterr().out
    assert "THE VIDEO STANDS." in out
    assert "PASS 4. block 1:" in out and "PASS 5. block 3:" in out
    assert "PASS 2. the policy is enforced" in out and "PASS 2. production's /api/products answered 200" in out
    assert "18 PASS and 0 FAIL" in out and "\nFAIL" not in out
    assert "[thirty-eight], 1.10.0 back at: 0:38, 0:38, 0:38." in out
    assert day.log[:2] == ["baseline", "release 1.10.0"]


def test_a_bad_day_no_video_exit_1_and_the_fix_never_measured(capsys):
    day = Day(failing=[blip(restarted=2)] * 3, ladder=[blip(restarted=2)] * 3)
    assert measure(day) == 1
    out = capsys.readouterr().out
    assert "THE VIDEO ISN'T MADE" in out
    assert "ladder 5" in day.log
    assert "release 1.10.1" not in day.log


def test_the_ladder_saves_it_exit_0_and_says_what_changes(capsys):
    day = Day(failing=[blip(restarted=2)] * 3, ladder=[blip()] * 3)
    assert measure(day) == 0
    out = capsys.readouterr().out
    assert "FAIL 4. block 1:" in out and "PASS 4. periodSeconds 5, block 1:" in out
    assert "THE VIDEO STANDS." in out and '"Two lines"' in out


def test_a_block_cut_short_is_fixed_before_the_ladder(capsys):
    day = Day(failing=[blip(restarted=2, block_seconds=29.0)] * 3)
    assert measure(day) == 1
    out = capsys.readouterr().out
    assert "under 30" in out and "ladder 5" not in day.log


def test_the_fix_failing_is_no_video_until_fixed_exit_1(capsys):
    day = Day(fixed=[blip("1.10.1", restarted=1)] * 3)
    assert measure(day) == 1
    assert "NO VIDEO UNTIL FIXED" in capsys.readouterr().out


def test_a_block_that_isnt_enforced_fails_check_2_exit_2(capsys):
    day = Day(connection="connected 0.01")
    assert measure(day) == 2
    out = capsys.readouterr().out
    assert "FAIL 2. the policy is enforced on this node" in out
    assert "THE VIDEO STANDS." in out and "fix these before a take" in out


def test_a_cluster_that_isnt_set_up_says_so_and_starts_nothing(capsys):
    day = Day(problems=["catalog-postgres-0 isn't ready in staging: scripts/demo.sh up database-blip"])
    assert measure(day) == 1
    assert "FAIL Not started: catalog-postgres-0 isn't ready" in capsys.readouterr().out
    assert day.log == []


def test_measuring_a_period_directly_goes_straight_there(capsys):
    day = Day(ladder=[blip()] * 3)
    day.failing = day.ladder
    assert measure(day, liveness_period=5) == 0
    assert day.log[:2] == ["baseline", "ladder 5"]
    assert "periodSeconds: 5" in capsys.readouterr().out


def test_every_line_of_a_days_output_says_pass_or_fail_or_is_a_heading(capsys):
    measure(Day())
    for line in capsys.readouterr().out.splitlines():
        assert re.match(r"(PASS|FAIL) |== |   |-- |     ", line), line


def test_the_results_are_json_safe_for_the_log():
    assert json.dumps(blip())
