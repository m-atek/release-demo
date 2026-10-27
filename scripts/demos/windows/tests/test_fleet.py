"""The PowerShell each step sends, the answers it reads, the Slack rows and
the fleet check's expectations, without AWS.

    .venv/bin/python -m pytest -q scripts/demos/windows/tests
"""

import sys
from pathlib import Path

# The shared scripts first, then this demo's, which win on a name both have (deploy, check).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check  # noqa: E402
import deploy  # noqa: E402
import fleet  # noqa: E402
from common import service  # noqa: E402


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
