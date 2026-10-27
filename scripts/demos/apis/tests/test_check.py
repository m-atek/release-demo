import check


def state(version="2.1.1", canary="ready", option=""):
    return {"version": version, "option": option,
            "deploys": {"production": {"number": "2", "canary": {"state": canary}}}}


def test_the_production_check_runs_the_canary_of_this_run_only():
    assert check.canary_due(state(), "production", "2.1.1")
    assert not check.canary_due(state(), "staging", "2.1.1")
    assert not check.canary_due(state(), "production", "2.0.4")      # the check after a rollback
    assert not check.canary_due(state(canary="pulled-back"), "production", "2.1.1")
    assert not check.canary_due({"version": "2.1.1"}, "production", "2.1.1")


def test_calls_get_the_shorter_canary():
    assert check.canary_minutes(state()) == 5
    assert check.canary_minutes(state(option="call")) == 3
