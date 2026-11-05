"""reset.py and demo.py against fakes: order, what a dry run leaves alone, what's deleted, what asks first."""

import base64
import contextlib
import io
import os
import subprocess
import sys
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest

import contexts
import demo
import host
import kube
import on_host
import remote
import reset

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]


class Shared:
    """scripts/reset.py, as shared_reset() returns it."""

    class HistoryRunning(Exception):
        pass

    def __init__(self, log):
        self.log = log
        self.jobs = []

    class Reset:
        def __init__(self, dry_run):
            self.dry_run, self.actions, self.failed = dry_run, 0, []

        def act(self, what, do):
            self.actions += 1
            if self.dry_run:
                print(f"  would {what}")
                return
            do()
            print(f"  {what}: done")

        def step(self, title, body):
            print(title)
            try:
                body()
            except Exception as error:
                self.failed.append(title)
                print(f"  failed: {error}")

    def release_jobs(self):
        return self.jobs

    def stop_runs(self, the_reset, jobs):
        self.log.append("stop runs")

    def cancel_changes(self, the_reset):
        self.log.append("cancel changes")

    def warm_slack_handler(self, the_reset, url):
        self.log.append(f"warm {url}")


@pytest.fixture
def world(monkeypatch):
    log = []
    state = {"policy": True, "short": {"production": 0}, "elsewhere": ["production"], "main": "1.8.2", "refusal": None}
    shared = Shared(log)
    monkeypatch.setattr(reset, "shared_reset", lambda: shared)
    monkeypatch.setattr(on_host, "refusal", lambda: state["refusal"])
    monkeypatch.setattr(on_host, "policy_on", lambda: state["policy"])
    monkeypatch.setattr(on_host, "binding_on", lambda: state["policy"])

    def remove_policy():
        log.append("delete policy")
        state["policy"] = False

    monkeypatch.setattr(on_host, "remove_policy", remove_policy)
    monkeypatch.setattr(on_host, "short_of_pods", lambda: dict(state["short"]))

    def restore(namespace):
        log.append(f"scale {namespace}")
        state["short"].pop(namespace)

    monkeypatch.setattr(on_host, "restore", restore)

    @contextlib.contextmanager
    def credentials(quiet=False):
        yield "kubeconfig"

    monkeypatch.setattr(kube, "credentials", credentials)
    monkeypatch.setattr(kube, "Api", lambda path: "api")
    monkeypatch.setattr(reset, "elsewhere", lambda kubeconfig, api, config: list(state["elsewhere"]))

    def put_on_baseline():
        log.append("helm baseline")
        state["elsewhere"] = []

    monkeypatch.setattr(reset, "put_on_baseline", put_on_baseline)
    monkeypatch.setattr(host, "main_version", lambda path: state["main"])

    def stage(name, version):
        log.append(f"stage {name} {version}")
        state["main"] = version

    monkeypatch.setattr(host, "stage", stage)
    return Namespace(log=log, state=state)


def args(**values):
    return Namespace(dry_run=False, slack_handler_url="https://example.invalid/warm", **values)


def test_it_puts_everything_back_in_order_and_ends_ready(world, capsys):
    assert reset.run(args()) == 0
    assert world.log == ["stop runs", "delete policy", "cancel changes", "scale production", "helm baseline",
                         "stage catalog 1.9.1", "warm https://example.invalid/warm"]
    assert "Ready for a take." in capsys.readouterr().out


def test_the_count_comes_back_before_helm_runs_because_helm_would_conflict(world):
    reset.run(args())
    assert world.log.index("scale production") < world.log.index("helm baseline")


def test_a_second_run_finds_nothing_to_do(world, capsys):
    reset.run(args())
    world.log.clear()
    assert reset.run(args()) == 0
    assert world.log == ["stop runs", "cancel changes", "warm https://example.invalid/warm"]
    assert "0 action(s)" in capsys.readouterr().out


def test_a_dry_run_changes_nothing(world, capsys):
    assert reset.run(Namespace(dry_run=True, slack_handler_url=None)) == 0
    assert "delete policy" not in world.log and "scale production" not in world.log and "helm baseline" not in world.log
    out = capsys.readouterr().out
    assert "would delete the binding no-zero and the policy no-zero" in out
    assert "would scale catalog in production from 0 to 3 pods" in out
    assert "would stage catalog 1.9.1" in out
    assert "Ready for a take" not in out


def test_it_refuses_and_touches_nothing_if_something_is_in_the_way(world, capsys):
    world.state["refusal"] = "video 33's forecast is up and fills the node"
    assert reset.run(args()) == 1
    assert world.log == []
    assert "Refused: video 33's forecast" in capsys.readouterr().out


def test_a_step_that_fails_says_so_and_the_exit_code_too(world, capsys, monkeypatch):
    def broken(name, version):
        raise RuntimeError("no push")

    monkeypatch.setattr(host, "stage", broken)
    assert reset.run(args()) == 1
    assert "these steps failed" in capsys.readouterr().out


def test_the_versions_are_ones_that_exist_and_no_new_one():
    assert (ROOT / "demo-states" / "catalog" / reset.BASELINE).is_dir()
    assert (ROOT / "demo-states" / "catalog" / reset.TAKE).is_dir()
    assert reset.BASELINE == "1.8.2" and reset.TAKE == "1.9.1"


def test_the_shared_reset_loads_from_this_folder_as_the_real_run_loads_it():
    """scripts/reset.py imports check, host, history and others by name, with this folder first on the path:
    a check.py here once shadowed scripts/check.py, and the reset would have stopped at its first import."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "import runpy; module = runpy.run_path(sys.argv[1] + '/reset.py', run_name='not_main');"
            "shared = module['shared_reset']();"
            "assert shared.SERVICE == 'catalog' and shared.BASELINE == '1.8.2';"
            "assert callable(shared.wait_for_version) and callable(shared.cancel_changes) and callable(shared.stop_runs);"
            "assert callable(shared.warm_slack_handler); print('loaded')")
    result = subprocess.run([sys.executable, "-c", code, str(HERE)], capture_output=True, text=True, cwd=ROOT,
                            env={**os.environ, "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                                 "AWS_EC2_METADATA_DISABLED": "true"})
    assert result.returncode == 0 and "loaded" in result.stdout, result.stderr


def test_no_file_here_shadows_what_the_shared_reset_imports():
    scripts = {path.stem for path in (ROOT / "scripts").glob("*.py")}
    demo_8 = {path.stem for path in (ROOT / "scripts" / "demos" / "kubernetes").glob("*.py")}
    own = {path.stem for path in HERE.glob("*.py")}
    assert own & (scripts | demo_8) == {"reset"}  # reset loads the shared one by path, under its own name


def test_it_never_archives_other_videos_tags_or_jira_versions():
    text = Path(reset.__file__).read_text()
    assert "github_takes" not in text and "jfrog_takes" not in text and "jira_versions" not in text


def test_from_the_mac_it_hands_itself_over(monkeypatch):
    asked = {}

    class Module:
        @staticmethod
        def slack_handler_url():
            return "https://example.invalid/warm"

    monkeypatch.setattr(host, "on_host", lambda: False)
    monkeypatch.setattr(reset, "shared_reset", lambda: Module)
    monkeypatch.setattr("sys.argv", ["reset.py", "--dry-run"])

    def hand_over(name, script, arguments, title=None):
        asked.update(name=name, script=script, arguments=arguments, title=title)
        return 0

    monkeypatch.setattr(host, "hand_over", hand_over)
    assert reset.main() == 0
    assert asked["name"] == "reset" and asked["script"] == "demos/one_wrong_context/reset.py"
    assert asked["arguments"] == ["--dry-run", "--slack-handler-url", "https://example.invalid/warm"]
    assert (ROOT / "scripts" / asked["script"]).exists()


def fake_remote(monkeypatch, code=0, output="done\n"):
    sent = []

    def run(command, timeout=600):
        sent.append(command)
        return code, output

    monkeypatch.setattr(remote, "run", run)
    return sent


def test_down_says_what_it_deletes_and_asks_first(monkeypatch, tmp_path, capsys):
    sent = fake_remote(monkeypatch)
    monkeypatch.setattr(demo, "running", lambda: True)
    copy = tmp_path / "copy"
    copy.write_text("x")
    monkeypatch.setattr(contexts, "COPY", copy)
    assert demo.down(ask=lambda prompt: "no") == 1
    out = capsys.readouterr().out
    assert "ValidatingAdmissionPolicyBinding no-zero" in out and "ValidatingAdmissionPolicy no-zero" in out
    assert "~/.kube/release-demo/one-wrong-context" in out
    assert "Nothing removed." in out
    assert sent == [] and copy.exists()


def test_down_with_a_yes_removes_exactly_that(monkeypatch, tmp_path, capsys):
    sent = fake_remote(monkeypatch)
    monkeypatch.setattr(demo, "running", lambda: True)
    copy = tmp_path / "copy"
    copy.write_text("x")
    monkeypatch.setattr(contexts, "COPY", copy)
    assert demo.down(yes=True) == 0
    assert sent == ["python3 scripts/demos/one_wrong_context/on_host.py remove"]
    assert not copy.exists()


def test_down_with_the_host_stopped_says_what_stays(monkeypatch, tmp_path, capsys):
    sent = fake_remote(monkeypatch)
    monkeypatch.setattr(demo, "running", lambda: False)
    monkeypatch.setattr(contexts, "COPY", tmp_path / "copy")
    assert demo.down(yes=True) == 0
    assert sent == []
    assert "stays on the cluster's disk" in capsys.readouterr().out


def test_a_failed_remove_keeps_the_mac_copy_and_says_so(monkeypatch, tmp_path):
    fake_remote(monkeypatch, code=1, output="no\n")
    monkeypatch.setattr(demo, "running", lambda: True)
    copy = tmp_path / "copy"
    copy.write_text("x")
    monkeypatch.setattr(contexts, "COPY", copy)
    with pytest.raises(SystemExit):
        demo.down(yes=True)
    assert copy.exists()


def test_status_only_reads(monkeypatch, capsys):
    sent = fake_remote(monkeypatch, output="policy no-zero: off\n")
    monkeypatch.setattr(demo, "running", lambda: True)
    assert demo.status() == 0
    assert sent == ["python3 scripts/demos/one_wrong_context/on_host.py status"]


def test_status_with_the_host_stopped(monkeypatch, capsys):
    sent = fake_remote(monkeypatch)
    monkeypatch.setattr(demo, "running", lambda: False)
    assert demo.status() == 0
    assert sent == [] and "stopped" in capsys.readouterr().out


def test_demo_sh_can_find_each_script():
    for name in ("down.sh", "status.sh", "pods.sh"):
        path = HERE / name
        assert path.exists() and path.stat().st_mode & 0o100, name
    assert not (HERE / "up.sh").exists()  # nothing of its own to start: scripts/demo.sh up kubernetes brings the cluster


def own_bundle():
    return remote.host.bundle(folders=remote.FILES)


def test_the_bundle_stays_under_ssms_limit_and_holds_its_two_files():
    files = own_bundle()
    assert len(files) < remote.host.LIMIT
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(files))).getnames()
    assert set(remote.FILES) <= set(names)
    assert "scripts/demos/one_wrong_context/first_day.py" not in names  # the Mac's scripts stay on the Mac
    # This demo adds little to what video 8's commands already send, so it costs the next video no room.
    assert len(files) - len(remote.host.bundle()) < 6_000


def test_video_8s_own_commands_send_what_they_always_sent():
    usual = list(remote.host.FILES)
    own_bundle()
    assert remote.host.FILES == usual
