"""The wave logic without AWS: the plan, the pre-checks, the parallel limit,
the stop rule, and the texts Slack and the change show.

    .venv/bin/python -m pytest -q scripts/demos/windows/tests
"""

import datetime
import sys
import threading
import time
from pathlib import Path

# The shared scripts first, then this demo's, which win on a name both have (deploy, check).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import waves  # noqa: E402

WAVES = [["STORE-001"], ["STORE-002", "STORE-003"], ["STORE-004", "STORE-005"], ["STORE-006"]]
ALL_OLD = {f"STORE-00{i}": "3.2.0" for i in range(1, 7)}


def test_a_release_plans_every_server_in_its_wave():
    assert waves.plan(WAVES, ALL_OLD, "3.3.0") == WAVES


def test_resume_plans_only_the_servers_not_on_the_new_version():
    versions = {**ALL_OLD, "STORE-001": "3.3.0", "STORE-002": "3.3.0", "STORE-003": "3.3.0", "STORE-005": "3.3.0"}
    assert waves.plan(WAVES, versions, "3.3.0", resume=True) == [["STORE-004"], ["STORE-006"]]


def test_resume_with_everything_done_plans_nothing():
    assert waves.plan(WAVES, {n: "3.3.0" for n in ALL_OLD}, "3.3.0", resume=True) == []


def at(text, zone="Europe/Amsterdam"):
    from zoneinfo import ZoneInfo
    return datetime.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone))


def test_the_demo_window_is_always_open():
    for moment in ("2026-10-26T00:00", "2026-10-28T13:30", "2026-11-01T23:59"):
        assert waves.in_window("Mon-Sun 00:00-24:00", "Europe/Amsterdam", at(moment))


def test_a_night_window_runs_past_midnight():
    window = "Mon-Fri 22:00-06:00"
    assert waves.in_window(window, "Europe/Amsterdam", at("2026-10-27T23:00"))      # Tuesday night
    assert waves.in_window(window, "Europe/Amsterdam", at("2026-10-28T05:59"))      # early Wednesday
    assert not waves.in_window(window, "Europe/Amsterdam", at("2026-10-28T12:00"))  # Wednesday noon
    assert waves.in_window(window, "Europe/Amsterdam", at("2026-11-01T05:00")) is False  # Sunday: Saturday not in it


def test_the_window_is_the_stores_own_time():
    # 21:30 UTC is 22:30 in Amsterdam in October's winter time: inside 22:00-06:00.
    utc = datetime.datetime(2026, 10, 27, 21, 30, tzinfo=datetime.timezone.utc)
    assert waves.in_window("Mon-Fri 22:00-06:00", "Europe/Amsterdam", utc)


SERVER = {"store": "Eindhoven", "timezone": "Europe/Amsterdam", "maintenance_window": "Mon-Sun 00:00-24:00"}


def test_prechecks_pass_on_a_healthy_server():
    checks = waves.judge_prechecks({"free_gb": 10.1, "service": "Running", "pending_reboot": []}, SERVER, {"min_free_gb": 5})
    assert all(ok for _, ok in checks)
    assert [what for what, _ in checks][0] == "free disk 10.1 GB (at least 5)"


def test_prechecks_fail_on_disk_service_and_reboot():
    checks = dict(waves.judge_prechecks({"free_gb": 2, "service": "Stopped", "pending_reboot": ["Windows Update"]},
                                        SERVER, {"min_free_gb": 5}))
    assert checks == {"free disk 2 GB (at least 5)": False, "service Stopped": False,
                      "pending reboot: Windows Update": False,
                      "maintenance window Mon-Sun 00:00-24:00 Europe/Amsterdam: open": True}


def fake_upgrade(fail_on=(), seconds=0.05):
    """An upgrade that records how many run at once."""
    running, peak, order, lock = [0], [0], [], threading.Lock()

    def upgrade(name):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            order.append(name)
        time.sleep(seconds * (3 if name == "STORE-005" else 1))
        with lock:
            running[0] -= 1
        return {"server": name, "failed": name in fail_on}
    return upgrade, peak, order


def test_every_server_runs_and_never_more_than_the_limit():
    upgrade, peak, order = fake_upgrade()
    results, stopped_by = waves.run(WAVES, upgrade, parallel=2)
    assert stopped_by is None
    assert sorted(results) == sorted(ALL_OLD)
    assert peak[0] == 2
    assert order[0] == "STORE-001"


def test_the_canary_runs_alone():
    events = []
    upgrade, _, _ = fake_upgrade()
    waves.run(WAVES, upgrade, parallel=2, on_wave=lambda n, names, status: events.append((n, status)))
    assert events == [(1, "running"), (1, "done"), (2, "running"), (2, "done"),
                      (3, "running"), (3, "done"), (4, "running"), (4, "done")]


def test_a_limit_of_one_runs_one_at_a_time():
    upgrade, peak, _ = fake_upgrade()
    waves.run(WAVES, upgrade, parallel=1)
    assert peak[0] == 1


def test_the_stop_rule_lets_running_servers_finish_and_starts_no_new_one():
    upgrade, _, order = fake_upgrade(fail_on={"STORE-004"})
    events = []
    results, stopped_by = waves.run(WAVES, upgrade, parallel=2,
                                    on_wave=lambda n, names, status: events.append((n, status)))
    assert stopped_by == "STORE-004"
    assert "STORE-005" in results            # it was already running: it finishes
    assert "STORE-006" not in results        # the next wave never starts
    assert "STORE-006" not in order
    assert events[-1] == (3, "stopped")


def test_a_failure_in_a_wave_wider_than_the_limit_starts_no_queued_server():
    upgrade, _, order = fake_upgrade(fail_on={"STORE-001"})
    results, stopped_by = waves.run([["STORE-001", "STORE-002", "STORE-003"]], upgrade, parallel=1)
    assert stopped_by == "STORE-001"
    assert order == ["STORE-001"]


def test_the_slack_line_is_the_one_the_video_shows():
    line = waves.slack_line({"server": "STORE-004", "before": "3.2.0", "failed_at": "check", "rollback_ok": True}, "3.3.0")
    assert line == "STORE-004 failed its check, back on 3.2.0. Wave stopped."


def test_other_stops_say_what_happened():
    assert waves.slack_line({"server": "STORE-002", "failed_at": "prechecks"}, "3.3.0") == \
        "STORE-002 failed its pre-checks, not touched. Wave stopped."
    assert waves.slack_line({"server": "STORE-002", "before": "3.2.0", "failed_at": "download"}, "3.3.0") == \
        "STORE-002 couldn't get a verified 3.3.0 package, still on 3.2.0. Wave stopped."
    assert "going back to 3.2.0 failed too" in waves.slack_line(
        {"server": "STORE-004", "before": "3.2.0", "failed_at": "check", "rollback_ok": False}, "3.3.0")


def test_the_work_note_has_everything_the_change_needs():
    note = waves.work_note({
        "server": "STORE-004", "store": "Eindhoven", "wave": 3, "status": waves.ROLLED_BACK,
        "before": "3.2.0", "after": "3.2.0", "prechecks": [("free disk 10.1 GB (at least 5)", True)],
        "package": "demos-candidates/store-sync/3.3.0/12/store-sync-3.3.0.zip", "sha256": "ab" * 32,
        "health": "/health answered 503 with 3.3.0 after 16.2s, expected 3.3.0: UploadEndpoint is not set",
        "rollback": "3.2.0 back and healthy after 4.1s", "seconds": 62.2, "commands": ["c1", "c2"]}, "3.3.0")
    assert note.splitlines() == [
        "STORE-004 (Eindhoven), wave 3: failed its check, back on 3.2.0",
        "Pre-checks: free disk 10.1 GB (at least 5) ok",
        "Versions: 3.2.0 before, 3.2.0 after",
        f"Package: demos-candidates/store-sync/3.3.0/12/store-sync-3.3.0.zip, SHA-256 {'ab' * 32} verified on the server",
        "Health: /health answered 503 with 3.3.0 after 16.2s, expected 3.3.0: UploadEndpoint is not set",
        "Rollback: 3.2.0 back and healthy after 4.1s",
        "Duration: 1m 03s",
        "Run Command: c1, c2",
    ]


def test_the_summary_counts_the_fleet():
    results = {"STORE-001": {"status": waves.HEALTHY}, "STORE-002": {"status": waves.HEALTHY},
               "STORE-003": {"status": waves.HEALTHY}, "STORE-005": {"status": waves.HEALTHY},
               "STORE-004": {"status": waves.ROLLED_BACK, "after": "3.2.0", "reason": "UploadEndpoint is not set"}}
    assert waves.summary(results, WAVES, "3.3.0", "STORE-004") == (
        "4 of 6 servers on 3.3.0. STORE-004 failed (UploadEndpoint is not set) and is on 3.2.0. Not started: STORE-006.")
