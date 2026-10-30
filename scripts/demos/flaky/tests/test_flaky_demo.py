"""Demo 15's own pieces: the demo states, the calibration, the dashboard, the
reset and the two Jenkins jobs. GitHub and Jira are fakes.

    .venv/bin/python -m pytest -q scripts/demos/flaky/tests
"""

import base64
import datetime
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(ROOT / "scripts"))

import calibrate  # noqa: E402
import quarantine  # noqa: E402
import quiet  # noqa: E402

STATES = ROOT / "demo-states" / "orders-service"
DASHBOARD = json.loads((HERE / "dashboard.json").read_text())


def load_reset():
    spec = importlib.util.spec_from_file_location("flaky_reset", HERE / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pytest_in(state, *args):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args, "tests"],
                          cwd=STATES / state, capture_output=True, text=True)


def test_1_5_0_passes_and_1_5_1_fails_only_its_per_region_test():
    assert pytest_in("1.5.0", "-k", "not timeout").returncode == 0
    broken = pytest_in("1.5.1", "-k", "not timeout")
    assert broken.returncode == 1
    failed = re.findall(r"^FAILED (\S+)", broken.stdout, re.MULTILINE)
    assert failed == ["tests/test_handler.py::test_delivery_estimate_per_region"]


def test_the_timing_test_asserts_a_limit_and_records_its_time():
    for state in ("1.5.0", "1.5.1"):
        text = (STATES / state / "tests" / "test_handler.py").read_text()
        assert calibrate.LIMIT_LINES.search(text), state
        assert "def test_delivery_estimate_timeout(record_property):" in text
        assert "assert elapsed < TIME_LIMIT_SECONDS" in text
    assert (STATES / "1.5.0" / "VERSION").read_text().strip() == "1.5.0"
    assert (STATES / "1.5.1" / "VERSION").read_text().strip() == "1.5.1"


def test_the_states_carry_no_demo_disclaimer():
    for state in ("1.5.0", "1.5.1"):
        assert "Demo system" not in (STATES / state / "handler.py").read_text()


def test_the_container_runs_calibrate_where_its_paths_resolve():
    # The toolchain container runs calibrate.py from CONTAINER_SCRIPT. Its
    # module-level paths must resolve there: from /calibrate they didn't.
    namespace = {"__file__": calibrate.CONTAINER_SCRIPT, "__name__": "calibrate_in_container"}
    exec(compile((HERE / "calibrate.py").read_text(), calibrate.CONTAINER_SCRIPT, "exec"), namespace)
    assert str(namespace["SCRIPTS"]) == "/release-demo/scripts"
    with pytest.raises(IndexError):
        exec(compile((HERE / "calibrate.py").read_text(), "/calibrate/calibrate.py", "exec"),
             {"__file__": "/calibrate/calibrate.py", "__name__": "calibrate_at_root"})


def test_percentiles_and_the_limit_lines():
    times = [i / 100 for i in range(1, 101)]
    assert calibrate.percentile(times, 0.5) == 0.5
    assert calibrate.percentile(times, 0.9) == 0.9
    assert calibrate.percentile(times, 0.99) == 0.99
    lines = calibrate.limit_lines(times, "the tools host")
    figures, limit = lines.splitlines()
    assert figures.startswith("# Measured: p50 0.500 s, p90 0.900 s, p99 0.990 s in 100 runs on the tools host, ")
    assert limit == "TIME_LIMIT_SECONDS = 0.900"


def test_write_replaces_the_limit_in_both_states(tmp_path, monkeypatch):
    for state in ("1.5.0", "1.5.1"):
        (tmp_path / state / "tests").mkdir(parents=True)
        (tmp_path / state / "tests" / "test_handler.py").write_text(
            (STATES / state / "tests" / "test_handler.py").read_text())
    monkeypatch.setattr(calibrate, "STATES", tmp_path)
    monkeypatch.setattr(calibrate, "ROOT", tmp_path)
    calibrate.write("# Measured: p50 0.300 s, p90 0.340 s, p99 0.400 s in 200 runs on the tools host, 2 Nov 2026.\n"
                    "TIME_LIMIT_SECONDS = 0.340")
    for state in ("1.5.0", "1.5.1"):
        text = (tmp_path / state / "tests" / "test_handler.py").read_text()
        assert "TIME_LIMIT_SECONDS = 0.340\n" in text and text.count("TIME_LIMIT_SECONDS = ") == 1


def test_the_dashboard_shows_what_the_video_reads():
    titles = {panel.get("title"): panel for panel in DASHBOARD["panels"]}
    assert DASHBOARD["uid"] == "release-demo-flaky" and DASHBOARD["title"] == "Flaky tests"
    assert DASHBOARD["time"] == {"from": "now-14d", "to": "now"}
    assert {"Flaky tests, last 14 days", "Reruns avoided", "Minutes saved"} <= set(titles)
    columns = titles["Flaky tests, last 14 days"]["transformations"][3]["options"]["renameByName"].values()
    assert list(columns) == ["Test", "Runs", "Failed, passed on rerun", "Failed twice", "Quarantined until"]
    for title in ("Flaky tests, last 14 days", "Reruns avoided", "Minutes saved"):
        assert len(titles[title]["description"]) > 80  # each gives its formula
    assert DASHBOARD["panels"][0]["options"]["content"] == "orders-service is a fictional service."
    assert "DORA" not in json.dumps(DASHBOARD)
    assert 'kind="test"' in titles["Reruns avoided"]["targets"][0]["expr"]
    assert "unwrap saved_seconds" in titles["Minutes saved"]["targets"][0]["expr"]


class FakeGitHub:
    def __init__(self, text):
        self.text, self.puts = text, []

    def __call__(self, method, path, token, body=None, scheme="Bearer"):
        if method == "GET":
            return {"content": base64.b64encode(self.text.encode()).decode(), "sha": "abc"}
        self.puts.append(body)
        self.text = base64.b64decode(body["content"]).decode()
        return {}


class FakeJira:
    calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))

    def transition(self, key, target):
        self.calls.append(("transition", key, target))
        return True


def test_the_reset_ends_the_takes_entries_once_and_closes_their_issues(monkeypatch):
    reset = load_reset()
    entry = {"service": "orders-service", "test": "tests/test_handler.py::test_delivery_estimate_timeout",
             "issue": "ORD-95", "since": datetime.date(2026, 11, 2), "until": datetime.date(2026, 11, 16),
             "reason": "8 of 96 runs in 14 days failed, then passed on rerun."}
    github = FakeGitHub(quarantine.render([entry]))
    monkeypatch.setattr(reset, "github_api", github)
    monkeypatch.setattr(reset, "github_app_token", lambda ssm: ("token", "owner/release-demo", None))
    monkeypatch.setattr(reset, "ssm_client", lambda: None)
    import jira

    monkeypatch.setattr(jira, "Jira", FakeJira)
    shared = reset.shared_reset()
    actions = shared.Reset(dry_run=False)
    reset.end_quarantine(actions)
    (put,) = github.puts
    assert put["message"] == "demo: end the quarantine entries of earlier takes (ORD-95) [skip ci]"
    assert quarantine.load_text(github.text) == []
    assert ("transition", "ORD-95", "Done") in FakeJira.calls
    assert any("Earlier take" in json.dumps(body) for method, _, body in FakeJira.calls if method == "POST")
    again = shared.Reset(dry_run=False)
    reset.end_quarantine(again)
    assert again.actions == 0 and len(github.puts) == 1


def test_the_jobs_are_defined_as_the_spec_names_them():
    jobs = (ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    for name, script in (("orders-service-tests", "jenkins/jobs/orders-service-tests.Jenkinsfile"),
                         ("flaky-night", "jenkins/jobs/flaky-night.Jenkinsfile")):
        assert f"pipelineJob('{name}')" in jobs and f"fromRepository('{script}')" in jobs
        assert (ROOT / script).exists()
    tests = (ROOT / "jenkins" / "jobs" / "orders-service-tests.Jenkinsfile").read_text()
    assert 'currentBuild.displayName = "#${env.BUILD_NUMBER} orders-service ${params.VERSION} tests"' in tests
    assert "toolchain.test('orders-service', cfg, 'tests')" in tests and "unstable(" in tests
    assert "stage.py orders-service \"$VERSION\" --no-commit" in tests
    # A night's run waits for a quiet host before Build, outside the stages the video shows.
    gate = tests.index("python3 scripts/demos/flaky/quiet.py")
    assert tests.index("if (params.COMMIT) {\n        // flaky-night") < gate < tests.index("testsStage('Build')")
    night = (ROOT / "jenkins" / "jobs" / "flaky-night.Jenkinsfile").read_text()
    assert "started[0].userId != 'admin'" in night and "string(name: 'COMMIT', value: commit)" in night
    assert "stringParam('RUNS', '80'" in jobs and "stringParam('PAUSE_SECONDS', '60'" in jobs


class FakeHost:
    """/proc/stat of a 4-CPU host whose CPUs are shares[i] busy during sample i, and a clock."""

    def __init__(self, shares, builds=None):
        self.shares, self.builds_seen = list(shares), list(builds or [])
        self.busy = self.idle = 0.0
        self.now = 0.0
        self.samples = 0

    def read(self):
        return f"cpu  {int(self.busy)} 0 0 {int(self.idle)} 0 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 0 0\n"

    def sleep(self, seconds):
        share = self.shares[min(self.samples, len(self.shares) - 1)]
        self.samples += 1
        self.busy += share * seconds * 400
        self.idle += (1 - share) * seconds * 400
        self.now += seconds

    def builds(self):
        return self.builds_seen[min(self.samples, len(self.builds_seen) - 1)] if self.builds_seen else []

    def wait(self, **settings):
        said = []
        result = quiet.wait_until_quiet(builds=self.builds, read=self.read, sleep=self.sleep, clock=lambda: self.now,
                                        out=said.append, **settings)
        return result, said


def test_quiet_reads_the_share_of_all_cpus_that_worked():
    assert quiet.cpu_times("cpu  100 5 20 800 50 3 2 10 7 0\ncpu0 1 2 3 4 5 6 7 8 0 0\n") == (140, 990)
    host = FakeHost([0.25])
    assert quiet.busy_share(read=host.read, sleep=host.sleep) == pytest.approx(0.25, abs=0.001)


def test_a_quiet_host_starts_the_run_after_two_samples():
    (waited, ok), said = FakeHost([0.05]).wait()
    assert ok and waited == 20 and said == ["The host is quiet: its CPUs are 5% busy."]


def test_a_night_run_waits_while_ci_builds_even_when_its_cpus_look_quiet():
    # ci waiting on SonarQube's gate leaves the CPUs nearly idle, then builds the next service.
    host = FakeHost([0.7, 0.12, 0.6, 0.1, 0.08, 0.06], builds=[["ci #82"], ["ci #82"], ["ci #82"], [], [], []])
    (waited, ok), said = host.wait()
    assert ok and waited == 50
    assert said == ["Waited 0.8 minutes for a quiet host while ci #82 ran: its CPUs were up to 70% busy, now 8%."]


def test_a_host_that_stays_busy_runs_anyway_after_the_wait_and_says_so():
    (waited, ok), said = FakeHost([0.5], builds=[["release #61"]]).wait(wait_minutes=1)
    assert not ok and waited == 60
    assert said == ["Going on after 1 minutes: the host stayed busy (50% of its CPUs, release #61). "
                    "This run's timing test may fail for that reason."]


def test_without_jenkins_the_cpus_decide():
    host = FakeHost([0.3, 0.1, 0.1])
    host.builds = lambda: None
    (waited, ok), said = host.wait()
    assert ok and waited == 30


def test_the_night_has_room_for_its_waits():
    night = (ROOT / "jenkins" / "jobs" / "flaky-night.Jenkinsfile").read_text()
    assert "timeout(time: 12, unit: 'HOURS')" in night
