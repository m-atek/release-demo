"""The Blip check on a fake cluster with the chart's timing: its lines, its zero and its finally, its verdicts."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

import blip_check
import run_state
from fake_cluster import Cluster, check, config

DATA, ALIVE = "/api/products", "/health"


def run(cluster, http=None, **kwargs):
    the_check, said = check(cluster, http, **kwargs)
    return the_check.run(), said, the_check


def seconds_lines(said):
    return [line for line in said if " x" in line or ":" in line.split(" ")[-1]]


def test_the_failing_version_restarts_every_pod_and_stops_the_release():
    result, said, _ = run(Cluster("1.10.0"))
    assert result["passed"] is False
    assert result["restarted"] == 3
    assert result["slack_line"] == "Blip check failed: 3 of 3 pods restarted during a 30 s database block."
    assert result["rules"][0]["rule"] == "restarted"
    assert said[:2] == ["catalog 1.10.0, staging, 10 calls a second", "TIME  /api/products     /health"]
    assert said[2:7] == [f"-0:0{n} 200 x10           200 x10" for n in (5, 4, 3, 2, 1)]
    assert said[7] == "NetworkPolicy block-database: no way in"
    assert "0:00  database blocked" in said
    assert "0:30  database back" in said
    assert sum("failed liveness, restarting" in line for line in said) == 3
    assert any(line.endswith("all 3 pods not ready") for line in said)
    assert any("refused x10" in line for line in said)
    # The prediction's rows, on this model of the chart.
    assert 5 <= result["all_not_ready_at"] <= 11
    assert 31 <= result["back_at"] <= 43
    assert all(pod["restarts_after"] == pod["restarts_before"] + 1 for pod in result["pods"].values())
    assert not any(pod["crash_loop_back_off"] for pod in result["pods"].values())


def test_the_fixed_version_keeps_every_pod_and_answers_errors():
    result, said, _ = run(Cluster("1.10.1"))
    assert result["passed"] is True
    assert result["rules"] == []
    assert result["slack_line"] == "Blip check: 0 of 3 pods restarted during a 30 s database block."
    assert result["restarted"] == 0 and result["all_not_ready_at"] is None
    assert not any(pod["was_not_ready"] for pod in result["pods"].values())
    assert set(result["calls"][ALIVE]) == {"200"}
    assert set(result["calls"][DATA]) == {"200", "503"}
    assert set(result["calls_inside_block"][DATA]) == {"503"}
    assert "0:01  503 x10           200 x10" in said and "0:29  503 x10           200 x10" in said
    assert not [line for line in said if "refused" in line or "liveness" in line or "not ready" in line]
    assert 30 <= result["back_at"] <= 32


def test_every_line_fits_44_columns():
    for version in ("1.10.0", "1.10.1"):
        result, said, _ = run(Cluster(version))
        said.append(blip_check.end_line(result))
        assert max(len(line) for line in said) <= 44, [line for line in said if len(line) > 44]


@pytest.mark.parametrize("counts", [
    {200: 10}, {503: 10}, {"refused": 10}, {"timeout": 10}, {503: 6, "refused": 4}, {200: 5, 503: 3, "refused": 2},
    {200: 3, 503: 3, "refused": 2, "timeout": 2}, {200: 1, 500: 1, 503: 1, "refused": 1, "timeout": 11}, {}])
def test_a_line_with_any_mix_of_results_fits(counts):
    assert len(blip_check.cell(counts)) < blip_check.WIDTH  # a space between the two routes
    assert len(blip_check.row("0:38", blip_check.cell(counts), blip_check.cell(counts))) <= 44


def test_a_lines_time_is_when_it_prints_so_it_reads_the_clock():
    """The video's clock runs from the frame of 0:00: a line reading 0:12 prints in that second."""
    cluster = Cluster("1.10.1")
    printed = {}
    the_check, said = check(cluster)
    original = said.__class__.__call__

    def stamped(text):
        original(said, text)
        printed[text] = cluster.now

    the_check.say = stamped
    result = the_check.run()
    zero = printed["0:00  database blocked"]
    for line, at in printed.items():
        head = line.split(" ")[0]
        if head[0].isdigit() and ":" in head:
            shown = int(head[0]) * 60 + int(head[2:])
            assert int(at - zero) == shown, line
    assert blip_check.kube.clock(int(printed["0:30  database back"] - zero)) == "0:30"
    assert result["back_at"] is not None


def test_back_is_the_first_line_from_which_no_call_fails_again():
    result, said, _ = run(Cluster("1.10.0"))
    back = blip_check.kube.clock(result["back_at"])
    index = next(i for i, line in enumerate(said) if line.startswith(f"{back} ") and "x" in line)
    assert said[index].startswith(f"{back:<5} 200 x10")
    later = [line for line in said[index:] if " x" in line]
    assert later and all("refused" not in line and "503" not in line for line in later)
    assert blip_check.end_line(result) == f"Back at {back}. 3 of 3 pods restarted."


def test_zero_is_the_first_failed_call_not_the_apply():
    """With a slow policy the block still lasts its whole 30 s from the first failure."""
    result, said, the_check = run(Cluster("1.10.0", apply_lag=2.4))
    assert result["times"]["apply"] <= -2.4
    assert result["times"]["first_failure"] == 0
    assert 30.0 <= result["block_seconds"] < 30.2
    assert the_check.deleted_at - the_check.applied_at > 32


def test_the_whole_window_gives_every_pod_three_failures_at_any_phase():
    """Every liveness phase: 30 s from the first failure always holds three ticks."""
    for tenth in range(0, 100, 7):
        offsets = (tenth / 10, (tenth + 33) % 100 / 10, (tenth + 66) % 100 / 10)
        result, _, _ = run(Cluster("1.10.0", offsets=offsets))
        assert result["restarted"] == 3, offsets
        assert all(pod["restarts_after"] == 1 for pod in result["pods"].values()), offsets


def test_a_29_second_block_lets_a_pod_escape():
    """Why the check counts from the first failure: the spec's 10% per pod, 73% for all three."""
    escaped = 0
    for tenth in range(100):
        cluster = Cluster("1.10.0", offsets=(tenth / 10,), replicas=1)
        result, _, _ = run(cluster, settings=config(seconds=29, replicas=1), version="1.10.0")
        escaped += result["restarted"] == 0
    assert 5 <= escaped <= 15


def test_no_second_kill_and_no_back_off_within_the_block():
    result, _, the_check = run(Cluster("1.10.0"))
    assert not the_check.crash_loop
    assert sum(1 for event in the_check.api.cluster.events) == 3


def test_a_policy_left_behind_goes_first():
    cluster = Cluster("1.10.1")
    cluster.policy = True
    cluster.blocked_from = -5.0
    result, said, _ = run(cluster)
    assert said[0] == "block-database from an earlier run was still on: deleted."
    assert result["passed"]
    assert len(cluster.posted) == 1


def test_the_policy_is_the_one_write_and_it_goes_in_staging():
    cluster = Cluster("1.10.1")
    run(cluster)
    path, document = cluster.posted[0]
    assert path == "/apis/networking.k8s.io/v1/namespaces/staging/networkpolicies"
    assert document["metadata"] == {"name": "block-database", "namespace": "staging"}
    assert document["spec"] == {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "catalog-postgres"}},
                                "policyTypes": ["Ingress"], "ingress": []}


def test_the_delete_comes_thirty_seconds_after_the_first_failure():
    cluster = Cluster("1.10.1")
    result, _, the_check = run(cluster)
    during = [at for at, _ in cluster.deleted if at > the_check.applied_at]
    assert during[0] == pytest.approx(the_check.zero + 30, abs=0.01)
    assert result["times"]["delete_returned"] == pytest.approx(30, abs=0.01)


class Boom(Exception):
    pass


@pytest.mark.parametrize("error", [Boom("no"), SystemExit(143), KeyboardInterrupt()])
def test_an_error_in_the_block_cant_leave_the_policy_on(error):
    cluster = Cluster("1.10.1")
    the_check, _ = check(cluster)
    calls = []

    def http(port, route):
        calls.append(route)
        if len(calls) == 300:
            raise error
        return cluster.http(port, route)

    the_check.http = http
    with pytest.raises(type(error)):
        the_check.run()
    assert cluster.policy is False
    assert cluster.deleted[-1][1].endswith("/networkpolicies/block-database")


def test_a_failed_delete_is_tried_again():
    cluster = Cluster("1.10.1")
    the_check, _ = check(cluster)
    real = the_check.api.delete
    tries = []

    def flaky(path):
        tries.append(cluster.now)
        if cluster.policy and len(tries) < 4:
            raise OSError("no answer")
        return real(path)

    the_check.api.delete = flaky
    assert the_check.run()["passed"]
    assert cluster.policy is False


def test_a_policy_that_wont_go_is_said_out_loud():
    cluster = Cluster("1.10.1")
    the_check, said = check(cluster)
    real = the_check.api.delete
    deletes = []

    def stubborn(path):
        deletes.append(path)
        if len(deletes) > 1:
            raise OSError("no answer")
        return real(path)

    the_check.api.delete = stubborn
    with pytest.raises(OSError):
        the_check.run()
    assert said[-1].startswith("block-database IS STILL ON. Delete it: kubectl -n staging")


def result_with(http_change, **cluster_args):
    cluster = Cluster("1.10.1", **cluster_args)

    def http(port, route):
        return http_change(cluster, port, route) or cluster.http(port, route)

    return run(cluster, http)[0]


def in_block(cluster):
    return not cluster.database_answers()


def test_a_pass_count_with_health_failing_fails_on_health():
    result = result_with(lambda c, port, route: ("refused", False) if route == ALIVE and in_block(c) else None)
    assert result["passed"] is False
    assert result["restarted"] == 0
    assert result["slack_line"] == "Blip check failed: /health didn't answer during a 30 s database block."


@pytest.mark.parametrize("failure", ["refused", "timeout"])
def test_a_failure_in_the_block_other_than_a_503_fails_on_the_route(failure):
    result = result_with(lambda c, port, route: (failure, False) if route == DATA and in_block(c) else None)
    assert result["passed"] is False
    assert result["slack_line"] == "Blip check failed: /api/products didn't answer during a 30 s database block."


def test_a_503_with_data_in_it_fails():
    result = result_with(lambda c, port, route: (503, True) if route == DATA and in_block(c) else None)
    assert result["passed"] is False and result["data_in_a_503"] > 0


def test_a_database_that_comes_back_late_fails_on_the_route():
    result = result_with(lambda c, port, route: None, delete_lag=7.0)
    assert result["passed"] is False
    assert result["rules"][0]["rule"] == "recovery"
    assert result["slack_line"] == "Blip check failed: /api/products didn't answer during a 30 s database block."


def test_the_real_count_when_only_some_pods_restart():
    """Two pods with a tick that misses: the Slack line has the count that happened."""
    cluster = Cluster("1.10.0", offsets=(6.0, 1.3, 2.5))
    result, _, _ = run(cluster, settings=config(seconds=21))
    assert result["restarted"] == 1
    assert result["slack_line"] == (f"Blip check failed: {result['restarted']} of 3 pods restarted "
                                    "during a 21 s database block.")


def test_a_block_that_doesnt_take_fails_and_removes_the_policy():
    cluster = Cluster("1.10.1", apply_lag=99)
    result, said, _ = run(cluster)
    assert result["passed"] is False
    assert result["slack_line"] == "Blip check failed: the block didn't take."
    assert result["back_at"] is None
    assert cluster.policy is False
    assert "0:00  database blocked" not in said


def test_a_failure_before_the_block_applies_no_policy():
    cluster = Cluster("1.10.1")
    result, _, _ = run(cluster, lambda port, route: (503, False))
    assert result["passed"] is False
    assert result["slack_line"].endswith("didn't answer before the block.")
    assert cluster.posted == []


def test_pods_that_never_come_back_fail_at_the_end_of_the_recovery():
    cluster = Cluster("1.10.1")

    def http(port, route):
        return (503, False) if route == DATA and cluster.blocked_from is not None else cluster.http(port, route)

    result, said, _ = run(cluster, http)
    assert result["passed"] is False
    assert result["problem"] == "not back"
    assert cluster.now < 110  # 5 s baseline, 30 s block, 60 s recovery
    assert blip_check.end_line(result) == "Not back in time. 0 of 3 pods restarted."


def test_it_waits_for_a_late_pods_restart():
    """A pod killed just before the delete sits in its preStop while calls already answer."""
    result, _, _ = run(Cluster("1.10.0", offsets=(9.9, 9.8, 9.7)))
    assert result["restarted"] == 3


def test_pods_that_arent_ready_at_the_start_stop_it_before_any_block():
    cluster = Cluster("1.10.1")
    cluster.pods[0].ready = False
    cluster.pods[0].next_readiness = 10_000
    cluster.pods[0].next_liveness = 10_000
    the_check, _ = check(cluster)
    with pytest.raises(blip_check.NotReady, match="2 of 3 pods ready"):
        the_check.run()
    assert cluster.posted == []


def test_a_kill_read_from_an_event_counts_again_on_used_pods():
    """Kubernetes keeps one event and raises its count: a second block's kills still show."""
    cluster = Cluster("1.10.0")
    cluster.events.append({"metadata": {"name": "catalog-5d9f8b6c5-pod0.killing"},
                           "involvedObject": {"name": "catalog-5d9f8b6c5-pod0"}, "reason": "Killing", "count": 7,
                           "message": "Container catalog failed liveness probe, will be restarted"})
    the_check, said = check(cluster)
    the_check.prepare()
    assert the_check.events["catalog-5d9f8b6c5-pod0.killing"][1] == 7
    the_check.zero = 0.0
    the_check.observe(the_check.latest, {"catalog-5d9f8b6c5-pod0.killing": ("catalog-5d9f8b6c5-pod0", 8)})
    assert said[-1].endswith("pod0: failed liveness, restarting")


def test_blip_json_holds_every_call_and_the_times():
    result, _, the_check = run(Cluster("1.10.0"))
    assert json.loads(json.dumps(result))["every_call"]
    assert len(result["every_call"]) == len(the_check.calls)
    second, route, answer = result["every_call"][-1]
    assert route in (DATA, ALIVE) and answer == 200 and second > 30
    times = result["times"]
    assert times["apply"] < 0 < times["delete_returned"] and times["first_failure_utc"].endswith("Z")
    assert set(next(iter(result["pods"].values()))) == {
        "restarts_before", "restarts_after", "failed_liveness_at", "restarted_at", "crash_loop_back_off", "was_not_ready"}


def http_server_answers():
    """A real socket: a call that finds nothing listening counts as refused, one that hangs as timeout."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    return listener, port


def test_a_real_refused_call_and_a_real_time_out():
    listener, port = http_server_answers()
    listener.close()
    assert blip_check.http_call(port, DATA) == ("refused", False)
    listener, port = http_server_answers()
    listener.listen(1)
    started = time.monotonic()
    assert blip_check.http_call(port, DATA, timeout=0.2) == ("timeout", False)
    assert time.monotonic() - started < 1
    listener.close()


def test_a_real_503_with_and_without_data():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"error": "the catalog database is unavailable"}' if self.path == "/clean" else b'{"products": []}'
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert blip_check.http_call(server.server_address[1], "/clean") == (503, False)
        assert blip_check.http_call(server.server_address[1], "/dirty") == (503, True)
    finally:
        server.shutdown()


def test_the_settings_come_from_services_yaml():
    found = blip_check.settings("catalog")
    assert (found.environment, found.namespace, found.replicas, found.port) == ("staging", "staging", 3, 30081)
    assert (found.seconds, found.calls_per_second, found.data_route, found.alive_route, found.recover_seconds) == (
        30, 10, "/api/products", "/health", 60)
    assert blip_check.settings("catalog", "production").namespace == "production"


def adf(*lines):
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]}
                                                     for line in lines]}


STOPPED = "catalog 1.10.0, build #81: 3 of 3 pods restarted, back at 0:38. Release stopped."


def fake_jira(monkeypatch, comments=None, error=None):
    import jira

    class Fake:
        asked = []

        def call(self, method, path):
            self.asked.append((method, path))
            if error:
                raise error
            return {"comments": [{"body": adf(*text.split("\n"))} for text in comments]}

    monkeypatch.setattr(jira, "Jira", Fake)
    return Fake


def passed_result():
    return {"service": "catalog", "version": "1.10.1", "environment": "staging", "seconds": 30, "restarted": 0, "replicas": 3,
            "back_at": 31, "passed": True, "problem": None, "times": {"first_failure": 0},
            "slack_line": "Blip check: 0 of 3 pods restarted during a 30 s database block.", "rules": []}


def test_the_work_note_carries_both_runs(monkeypatch):
    fake = fake_jira(monkeypatch, ["Released catalog 1.9.1 in CHG1.", f"Blip check: {STOPPED}", "Blip check: catalog 1.10.0, "
                                   "build #70: 2 of 3 pods restarted, back at 0:35. Release stopped."])
    assert fake.asked == []
    earlier = blip_check.earlier_line("ORD-110")
    assert earlier == STOPPED
    assert fake.asked == [("GET", "/rest/api/3/issue/ORD-110/comment?orderBy=-created&maxResults=100")]
    assert blip_check.work_note(passed_result(), 82, earlier, DATA) == [
        "Blip check in staging: database blocked for 30 s.",
        "catalog 1.10.0, build #81: 3 of 3 pods restarted, back at 0:38. Release stopped.",
        "catalog 1.10.1, build #82: 0 of 3 pods restarted, back at 0:31. /api/products answered 503 until the database was back."]


def test_the_work_note_without_run_1s_comment_has_this_runs_line(monkeypatch):
    fake_jira(monkeypatch, ["Released catalog 1.9.1 in CHG1.", "Blip check: catalog 1.10.0, build #3: the block didn't take. Release stopped."])
    earlier = blip_check.earlier_line("ORD-110")
    assert earlier is None
    assert blip_check.work_note(passed_result(), 82, earlier, DATA) == [
        "Blip check in staging: database blocked for 30 s.",
        "catalog 1.10.1, build #82: 0 of 3 pods restarted, back at 0:31. /api/products answered 503 until the database was back."]


@pytest.mark.parametrize("error", [OSError("no answer"), SystemExit("Jira refused")])
def test_jira_down_never_stops_a_release_that_passed(monkeypatch, error, capsys):
    fake_jira(monkeypatch, error=error)
    assert blip_check.earlier_line("ORD-110") is None
    assert "this run's line only" in capsys.readouterr().out


def test_a_pass_sets_the_work_note_and_the_attachment_not_the_description(monkeypatch, tmp_path):
    fake_jira(monkeypatch, [f"Blip check: {STOPPED}"])
    run_state.save({"jira_key": "ORD-110", "build": {"number": 82}, "slack": {"channel": "C1", "ts": "1.2"}})
    calls = []
    monkeypatch.setattr(blip_check.subprocess, "run", lambda command, **kwargs: calls.append(command))
    path = tmp_path / ".release" / "blip.json"
    blip_check.record(config(), passed_result(), path)
    state = run_state.load()
    assert state["change_work_notes"][1] == STOPPED
    assert state["change_attachments"] == [str(path)]
    assert "change_details" not in state and "change_extras" not in state
    assert [command[1:] for command in calls] == [[str(blip_check.SCRIPTS / "notify.py"), "line", passed_result()["slack_line"]]]


def test_a_failure_says_so_in_slack_and_comments_on_the_issue(monkeypatch, tmp_path):
    run_state.save({"jira_key": "ORD-110", "build": {"number": 81}, "slack": {"channel": "C1", "ts": "1.2"}})
    calls = []
    monkeypatch.setattr(blip_check.subprocess, "run", lambda command, **kwargs: calls.append(command))
    result, _, _ = run(Cluster("1.10.0"))
    result["build"] = 81
    blip_check.record(config(), result, tmp_path / "blip.json")
    assert [command[1:3] for command in calls] == [[str(blip_check.SCRIPTS / "notify.py"), "line"],
                                                   [str(blip_check.SCRIPTS / "jira.py"), "comment"]]
    assert calls[0][3] == "Blip check failed: 3 of 3 pods restarted during a 30 s database block."
    back = blip_check.kube.clock(result["back_at"])
    assert calls[1][3] == f"Blip check: catalog 1.10.0, build #81: 3 of 3 pods restarted, back at {back}. Release stopped."
    assert blip_check.EARLIER.search(calls[1][3])
    assert "change_work_notes" not in run_state.load()


def test_a_comment_without_a_measurement_never_reaches_a_later_work_note(monkeypatch, tmp_path):
    run_state.save({"jira_key": "ORD-110", "build": {"number": 81}})
    calls = []
    monkeypatch.setattr(blip_check.subprocess, "run", lambda command, **kwargs: calls.append(command))
    result, _, _ = run(Cluster("1.10.1", apply_lag=99))
    blip_check.record(config(), result, tmp_path / "blip.json")
    text = calls[-1][3]
    assert text == "Blip check: catalog 1.10.1, build #81: the block didn't take. Release stopped."
    assert not blip_check.EARLIER.search(text)


def test_a_rehearsal_tells_nobody(monkeypatch, tmp_path, capsys):
    run_state.save({"service": "catalog", "version": "1.10.1", "jira_key": "ORD-110", "build": {"number": "rehearsal"},
                    "slack": {"channel": "C1", "ts": "1.2"}})
    monkeypatch.setattr(sys, "argv", ["blip_check.py", "catalog", "--rehearsal"])
    monkeypatch.setattr(blip_check, "LOG", tmp_path / "blip.log")
    monkeypatch.setattr(blip_check, "stop_on_sigterm", lambda: None)
    monkeypatch.setattr(blip_check.subprocess, "run", lambda *a, **k: pytest.fail("no Slack or Jira in a rehearsal"))
    monkeypatch.setattr(blip_check.Lines.__init__, "__defaults__", (tmp_path / "blip.log", None))
    cluster = Cluster("1.10.1")
    real = blip_check.Check.__init__

    def fake_check(self, config_, api, version, say, **kwargs):
        real(self, config_, cluster.api(), version, say, call=cluster.http, clock=cluster.clock, sleep=cluster.sleep,
             pool=__import__("fake_cluster").Pool())

    monkeypatch.setattr(blip_check.Check, "__init__", fake_check)
    monkeypatch.setattr(blip_check.kube, "credentials", lambda: __import__("contextlib").nullcontext("kubeconfig"))
    monkeypatch.setattr(blip_check.kube, "Api", lambda kubeconfig: None)
    assert blip_check.main() == 0
    written = json.loads((run_state.PATH.parent / "blip.json").read_text())
    assert written["passed"] and written["build"] == "rehearsal"
    assert "Back at" in (tmp_path / "blip.log").read_text()
    assert "Back at" in capsys.readouterr().out


def test_the_log_is_shared_between_root_and_the_agents_user(tmp_path):
    path = tmp_path / "blip.log"
    log = blip_check.open_log(path)
    log.write("one\n")
    log.close()
    assert oct(path.stat().st_mode & 0o777) == "0o666"
    log = blip_check.open_log(path)
    log.write("two\n")
    log.close()
    assert path.read_text() == "one\ntwo\n"


def test_a_log_it_cant_open_doesnt_stop_the_check(tmp_path, capsys):
    lines = blip_check.Lines(tmp_path / "no" / "such" / "folder" / "blip.log")
    lines("still printed")
    assert "still printed" in capsys.readouterr().out


HARNESS = """
import sys, time
sys.path[:0] = [{here!r}, {scripts!r}]
import blip_check
from fake_cluster import config
blip_check.stop_on_sigterm()

class Api:
    def pods(self, namespace, release):
        return [{{"metadata": {{"name": f"catalog-a-{{i}}"}}, "status": {{"containerStatuses": [{{"ready": True, "restartCount": 0}}]}}}}
                for i in range(3)]
    def get(self, path):
        return {{"items": []}}
    def post(self, path, document):
        print("policy on", flush=True)
    def delete(self, path):
        open({marker!r}, "a").write("deleted\\n")
        return True

def http(port, route):
    return (503, False) if route == "/api/products" and failing else (200, False)

failing = False
real_post = Api.post
def post(self, path, document):
    global failing
    failing = True
    real_post(self, path, document)
Api.post = post
blip_check.BASELINE_SECONDS = 1
blip_check.Check(config(), Api(), "1.10.1", lambda text: None, call=http).run()
"""


def test_jenkins_abort_a_real_sigterm_deletes_the_policy(tmp_path):
    marker = tmp_path / "marker"
    code = HARNESS.format(here=str(blip_check.HERE / "tests"), scripts=str(blip_check.SCRIPTS), marker=str(marker))
    env = {**os.environ, "PYTHONPATH": str(blip_check.HERE)}
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
    assert process.stdout.readline().strip() == "policy on"
    deletes_at_start = marker.read_text().count("deleted")  # the first step's look for a leftover
    time.sleep(0.3)
    process.send_signal(signal.SIGTERM)
    assert process.wait(20) == 143
    assert marker.read_text().count("deleted") == deletes_at_start + 1
