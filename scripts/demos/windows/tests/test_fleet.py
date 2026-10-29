"""The PowerShell each step sends, the answers it reads, the Slack rows, the
fleet check's expectations, the stop's close notes and the reset's warm-up,
without AWS.

    .venv/bin/python -m pytest -q scripts/demos/windows/tests
"""

import sys
from pathlib import Path

# The shared scripts first, then this demo's, which win on a name both have (deploy, check).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib.util  # noqa: E402

import fleet  # noqa: E402
from common import service  # noqa: E402


def demo_module(name):
    """This demo's deploy.py or check.py, by path: the shared scripts have modules of the same names."""
    spec = importlib.util.spec_from_file_location(f"windows_{name}", Path(__file__).resolve().parents[1] / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = demo_module("check")
deploy = demo_module("deploy")


def test_values_become_powershell_literals():
    assert fleet.ps_literal("C:\\StoreSync") == "'C:\\StoreSync'"
    assert fleet.ps_literal("it's") == "'it''s'"
    assert fleet.ps_literal(True) == "$true"
    assert fleet.ps_literal(None) == "$null"
    assert fleet.ps_literal(60) == "60"
    assert fleet.ps_literal({"i-1": {"name": "STORE-001", "endpoint": ""}}) == \
        "@{ 'i-1' = @{ 'name' = 'STORE-001'; 'endpoint' = '' } }"
    assert fleet.ps_literal(["a", "b"]) == "@('a', 'b')"


def test_a_step_is_its_values_then_common_then_the_step():
    text = fleet.script("health", Expected="3.3.0", TimeoutSeconds=60)
    assert text.startswith("$Expected = '3.3.0'\n$TimeoutSeconds = 60\n")
    assert text.index("function Wait-Health") < text.index("Write-Result (Wait-Health $Expected $TimeoutSeconds)")


def test_no_step_carries_a_secret_or_prints_the_token():
    for step in ("prechecks", "install", "health", "switch", "state", "config", "bootstrap"):
        text = fleet.script(step, TokenParameter="/release-demo/jcr/store-fleet-token")
        assert "Write-Host $token" not in text and "Say $token" not in text
    common = (fleet.POWERSHELL / "common.ps1").read_text()
    assert "$token = $null" in common


def test_the_result_is_the_last_result_line():
    output = "Downloading x\nRESULT {\"ok\": false}\nmore\nRESULT {\"ok\": true, \"sha256\": \"ab\"}\n"
    assert fleet.parse_result(output) == {"ok": True, "sha256": "ab"}
    assert fleet.parse_result("no result") is None


class FakeEc2:
    def describe_instances(self, **_):
        return {"Reservations": []}


def test_the_table_plants_the_drift_on_the_drifted_server_only():
    config = service("store-sync")
    f = fleet.Fleet.__new__(fleet.Fleet)
    f.env = config["environments"]["production"]
    rows = f.table({"STORE-003": "i-3", "STORE-004": "i-4"})
    assert rows["i-3"] == {"name": "STORE-003", "endpoint": "https://uploads.eu-west.store-sync.internal/STORE-003"}
    assert rows["i-4"] == {"name": "STORE-004", "endpoint": ""}
    assert f.table({"STORE-004": "i-4"}, drift=False)["i-4"]["endpoint"].endswith("/STORE-004")


def test_failures_are_described_in_one_line():
    result = fleet.Result("STORE-004", "i-4", "c", "Success", result={"ok": False, "code": 503, "version": "3.3.0",
                                                                      "problems": ["UploadEndpoint is not set"]})
    assert fleet.describe_failure(result) == "UploadEndpoint is not set"
    timed_out = fleet.Result("STORE-004", "i-4", "c", "TimedOut", error="line 1\nline 2")
    assert fleet.describe_failure(timed_out) == "Run Command TimedOut: line 2"


PLAN = {"waves": [["STORE-004"], ["STORE-006"]], "numbers": [3, 4],
        "servers": {"STORE-004": {"version": "3.2.0", "status": "rolled back", "before": "3.2.0"},
                    "STORE-006": {"version": "3.2.0", "status": "waiting", "before": "3.2.0"}},
        "others": {"STORE-001": "3.3.0"}}


def test_the_slack_rows_keep_the_wave_numbers():
    assert deploy.rows(PLAN) == ["Wave 3  STORE-004 3.2.0 rolled back", "Wave 4  STORE-006 3.2.0 waiting"]


def test_the_check_expects_where_the_run_left_each_server():
    state = {"plan": PLAN, "deploys": {"production": {"servers": {"STORE-004": {"after": "3.2.0"}}}}}
    assert check.expected_versions(state, ["STORE-001", "STORE-004", "STORE-006", "STORE-009"], "3.3.0") == {
        "STORE-001": "3.3.0", "STORE-004": "3.2.0", "STORE-006": "3.2.0", "STORE-009": "3.3.0"}


def test_after_back_the_check_expects_the_version_given():
    state = {"plan": PLAN, "deploys": {"production": {"servers": {"STORE-004": {"after": "3.3.0"}},
                                                      "rolled_back_servers": ["STORE-004"]}}}
    assert check.expected_versions(state, ["STORE-004"], "3.2.0") == {"STORE-004": "3.2.0"}


def test_without_a_run_every_server_must_report_the_version():
    assert check.expected_versions({}, ["STORE-001", "STORE-002"], "3.2.0") == {"STORE-001": "3.2.0", "STORE-002": "3.2.0"}


WAVES = [["STORE-001"], ["STORE-002", "STORE-003"], ["STORE-004", "STORE-005"], ["STORE-006"]]
RUN_1 = {"STORE-001": {"status": "healthy"}, "STORE-002": {"status": "healthy"}, "STORE-003": {"status": "healthy"},
         "STORE-005": {"status": "healthy"},
         "STORE-004": {"status": "rolled back", "after": "3.2.0", "reason": "UploadEndpoint is not set"}}


def stopped_run(tmp_path, monkeypatch, slack=True):
    """deploy.stopped for run 1 of the video, with the real notify.py in this process
    and a clock that moves 11 seconds at every reading, as the pipeline's steps take time.
    Returns what happened, in order: Slack's lines and the change's close."""
    import notify
    import run_state

    monkeypatch.setattr(run_state, "PATH", tmp_path / "state.json")
    clock = iter(range(1304, 2000, 11))  # Sam's command at 1000: the first reading is 5m 04s later
    monkeypatch.setattr(run_state, "now", lambda: next(clock))
    happened = []
    monkeypatch.setattr(notify, "post", lambda state: happened.append(("slack", state["lines"][-1])))

    def run_notify(command, check=False):
        monkeypatch.setattr(sys, "argv", ["notify.py", *command[2:]])
        notify.main()

    monkeypatch.setattr(deploy.subprocess, "run", run_notify)
    if not slack:
        monkeypatch.setenv("RELEASE_DEMO_SLACK", "off")

    class Change:
        @staticmethod
        def close(sn, state, code, notes):
            happened.append(("close", code, notes))

    state = {"service": "store-sync", "version": "3.3.0", "jira_key": "ORD-31", "started_at": 1000,
             "build": {"number": 23, "url": "https://jenkins/job/release/23/"}, "stages": {}, "lines": [],
             "plan": {"waves": WAVES, "others": {}}, "deploys": {"production": {"number": "3.3.0"}}}
    release = deploy.Release(service("store-sync"), state, fleet=object())
    release.sn, release.change = object(), Change
    state["change"] = {"number": "CHG0030031", "sys_id": "c1", "type": "normal"}
    deploy.stopped(release, RUN_1, "STORE-004")
    return happened, run_state.load()


def test_the_close_notes_give_the_total_of_slacks_last_line(tmp_path, monkeypatch):
    happened, saved = stopped_run(tmp_path, monkeypatch)
    assert [what[0] for what in happened] == ["slack", "close"]
    assert happened[0][1] == "Wave stopped at STORE-004 after 5m 04s. STORE-006 was not touched; the others run 3.3.0."
    code, notes = happened[1][1:]
    assert code == "successful_issues"
    assert "Total time: 5m 04s." in notes
    assert notes.startswith("4 of 6 servers on 3.3.0. STORE-004 failed (UploadEndpoint is not set) and is on 3.2.0. "
                            "Not started: STORE-006.")
    # The pipeline's stop sees both done: the total is set and the change is closed.
    assert saved["total"] == "5m 04s" and saved["change"]["closed"] == "successful_issues"


def test_without_slack_the_close_notes_still_give_a_total(tmp_path, monkeypatch):
    happened, saved = stopped_run(tmp_path, monkeypatch, slack=False)
    assert [what[0] for what in happened] == ["close"]
    assert "Total time: 5m 04s." in happened[0][2]
    assert "total" not in saved


def test_the_reset_warms_the_slack_handler_with_the_main_resets_step(monkeypatch, capsys):
    reset = demo_module("reset")
    names = ("check", "host")
    before = {name: sys.modules.get(name) for name in names}
    main = reset.main_reset()
    # The shared check.py and host.py, not this folder's, and this folder's names left as they were.
    scripts = Path(__file__).resolve().parents[3]
    assert Path(main.wait_for_version.__code__.co_filename) == scripts / "check.py"
    assert Path(main.host.__file__) == scripts / "host.py"
    assert {name: sys.modules.get(name) for name in names} == before

    calls = []
    monkeypatch.setattr(reset, "main_reset", lambda: main)
    monkeypatch.setattr(main, "warm_slack_handler", lambda step, url: calls.append(url))
    assert reset.warm_slack_handler() is True and calls == [None]
    monkeypatch.setattr(main, "warm_slack_handler", lambda step, url: main.fail("The Slack handler answered 500"))
    assert reset.warm_slack_handler() is False
    assert "The Slack handler, one call\n  failed" in capsys.readouterr().out
