import canary
import pricing
import run_state
from conftest import Clock, FakeCloudWatch, FakeLambda

FUNCTION = "pricing-api-production"


def release_state():
    state = {"service": "pricing-api", "version": "2.1.1", "option": "",
             "deploys": {"production": {"number": "2", "previous_number": "1", "previous_version": "2.0.4",
                                        "canary": {"state": "ready"}}}}
    run_state.save(state)
    return state


def run(fire_after=None, minutes=5):
    clock = Clock()
    lam = FakeLambda()
    cloudwatch = FakeCloudWatch(fire_after=None if fire_after is None else clock.now + fire_after, clock=clock)
    told, printed, loads = [], [], []
    state = release_state()
    result = canary.Canary(state, minutes, lam, cloudwatch, out=printed.append, tell=told.append,
                           load=lambda seconds, versions: loads.append((seconds, versions)), clock=clock, sleep=clock.sleep).run()
    return result, state, lam, cloudwatch, told, printed, loads, clock


def test_alarms_watch_the_new_version_only():
    specs = canary.alarm_specs(FUNCTION, "7", "2.1.1", pricing.config()["canary"])
    names = [s["AlarmName"].removeprefix(pricing.ALARM_PREFIX) for s in specs]
    assert names == ["errors-v7", "p95-v7"]
    for spec in specs:
        assert {"Name": "ExecutedVersion", "Value": "7"} in spec["Dimensions"]
        assert {"Name": "Resource", "Value": f"{FUNCTION}:live"} in spec["Dimensions"]
        assert spec["Period"] == 60
    assert specs[0]["MetricName"] == "Errors" and specs[0]["Statistic"] == "Sum"
    assert specs[1]["MetricName"] == "Duration" and specs[1]["ExtendedStatistic"] == "p95"


def test_thresholds_come_from_services_yaml():
    settings = pricing.config()["canary"]
    errors, p95 = canary.alarm_specs(FUNCTION, "7", "2.1.1", settings)
    assert errors["Threshold"] == settings["alarms"]["errors_per_minute"]
    assert p95["Threshold"] == settings["alarms"]["p95_duration_ms"]


def test_a_clean_canary_moves_live_fully_to_the_new_version():
    result, state, lam, cloudwatch, told, printed, loads, clock = run(minutes=5)
    assert result is True
    assert lam.updates[0] == ("1", {"2": 0.1})
    assert lam.updates[-1] == ("2", {})
    assert state["deploys"]["production"]["canary"]["state"] == "promoted"
    assert told[-1] == "Canary: 5 clean minutes at 10%. 2.1.1 takes all traffic."
    assert cloudwatch.alarms == {} and len(cloudwatch.deleted) == 2


def test_the_canary_runs_for_the_set_time():
    result, state, *_, clock = run(minutes=3)
    record = state["deploys"]["production"]["canary"]
    assert record["decided_at"] - record["started_at"] >= 3 * 60
    assert record["decided_at"] - record["started_at"] < 3 * 60 + 10


def test_an_alarm_takes_the_share_back_to_zero_within_seconds():
    result, state, lam, cloudwatch, told, printed, loads, clock = run(fire_after=95)
    assert result is False
    assert lam.main == "1" and lam.weights == {}
    record = state["deploys"]["production"]["canary"]
    assert record["state"] == "pulled-back"
    assert record["alarm_to_zero_seconds"] <= pricing.config()["canary"]["poll_seconds"] + 15
    assert record["alarm"]["what"] == "Errors"
    assert any(line.startswith("FAIL alarm pricing-api-canary-errors-v2") for line in printed)
    assert told[-1].startswith("Canary: the Errors alarm fired on 2.1.1. Its share of traffic was back at 0%")
    assert "share back to 0%" in state["check"]["notes"]


def test_no_alarm_remains_after_either_outcome():
    for fire_after in (None, 60):
        cloudwatch = run(fire_after=fire_after)[3]
        assert cloudwatch.alarms == {}


def test_an_interrupted_canary_takes_the_weight_back():
    clock = Clock()
    lam, cloudwatch = FakeLambda(), FakeCloudWatch(clock=clock)
    state = release_state()

    def interrupted(seconds):
        clock.sleep(seconds)
        if lam.weights:
            raise KeyboardInterrupt

    try:
        canary.Canary(state, 5, lam, cloudwatch, out=lambda *_: None, tell=lambda *_: None,
                      load=lambda seconds, versions: None, clock=clock, sleep=interrupted).run()
    except KeyboardInterrupt:
        pass
    assert lam.main == "1" and lam.weights == {}
    assert state["deploys"]["production"]["canary"]["state"] == "interrupted"
    assert cloudwatch.alarms == {}


def test_load_runs_through_the_canary_and_a_little_after():
    seconds, versions = run(minutes=5)[6][0]
    load = pricing.config()["canary"]["load"]
    assert seconds >= load["lead_seconds"] + 5 * 60 + load["tail_seconds"]
    assert versions == ["2.0.4", "2.1.1"]
